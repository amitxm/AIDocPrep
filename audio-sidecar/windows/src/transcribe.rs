//! Speech engines for the Windows `docprep-audio` sidecar — all in-process.
//!
//! * Whisper (ggml) via `whisper-rs`: Vulkan GPU for the large models when a
//!   capable GPU is present, CPU otherwise, with automatic CPU fallback if the
//!   GPU context fails to initialise.
//! * Parakeet TDT 0.6B v3 (NeMo transducer) via sherpa-onnx, CPU INT8.
//! * Speaker diarization (pyannote segmentation + CAM++ embeddings) via
//!   sherpa-onnx.
//!
//! Running inference in-process (rather than driving whisper-cli / sherpa CLI
//! executables) matters for two product requirements: per-file Skip kills this
//! single process cleanly with no orphaned grandchildren, and the model is
//! loaded once per file instead of once per 30-second chunk.

use std::env;
use std::ffi::{CStr, CString};
use std::io::Write;
use std::mem;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::align::{
    build_diarized_segments, build_undiarized_segments, round_millis, subword_tokens_to_words,
    TimedSpeakerSegment, WordTiming,
};
use crate::decode::{load_normalized_wav_f32, prepare_normalized_wav};
use crate::hardware::detect_hardware;
use crate::json::{emit_progress, emit_result};
use crate::SidecarError;

// ---------------------------------------------------------------------------
// Process helpers shared with probe.rs / decode.rs (ffprobe fallback etc.)
// ---------------------------------------------------------------------------

pub fn apply_no_window(_cmd: &mut Command) {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        _cmd.creation_flags(CREATE_NO_WINDOW);
    }
}

/// Candidate directories where bundled runtime DLLs and helper binaries live.
pub fn runtime_search_dirs() -> Vec<PathBuf> {
    let mut dirs = Vec::new();

    if let Ok(custom) = env::var("DOCPREP_AUDIO_RUNTIME_DIR") {
        let p = PathBuf::from(custom.trim());
        if p.is_dir() {
            dirs.push(p);
        }
    }

    if let Ok(exe_path) = env::current_exe() {
        if let Some(exe_dir) = exe_path.parent() {
            dirs.push(exe_dir.to_path_buf());
            dirs.push(exe_dir.join("_internal").join("audio_runtime"));
            if let Some(parent) = exe_dir.parent() {
                dirs.push(parent.join("Resources").join("_internal").join("audio_runtime"));
            }
        }
    }

    if let Ok(cwd) = env::current_dir() {
        dirs.push(cwd.join("desktop").join("src-tauri").join("binaries"));
    }

    dirs
}

pub fn find_runtime_or_system_exe(base_name: &str) -> Option<PathBuf> {
    let ext = if cfg!(target_os = "windows") { ".exe" } else { "" };
    let file_name = if base_name.ends_with(ext) {
        base_name.to_string()
    } else {
        format!("{}{}", base_name, ext)
    };

    for dir in runtime_search_dirs() {
        let candidate = dir.join(&file_name);
        if candidate.is_file() {
            return Some(candidate);
        }
    }

    if let Ok(path_var) = env::var("PATH") {
        let sep = if cfg!(target_os = "windows") { ';' } else { ':' };
        for part in path_var.split(sep) {
            if part.trim().is_empty() {
                continue;
            }
            let p = Path::new(part.trim()).join(&file_name);
            if p.is_file() {
                return Some(p);
            }
        }
    }

    None
}

// ---------------------------------------------------------------------------
// Model resolution
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum WindowsEngine {
    Whisper,
    SherpaParakeet,
}

pub fn resolve_model_and_engine(
    models_root: &Path,
    requested_model_id: &str,
) -> Result<(String, WindowsEngine, PathBuf), SidecarError> {
    let candidates: Vec<&str> = if !requested_model_id.trim().is_empty() {
        vec![requested_model_id.trim()]
    } else {
        vec![
            "whisper-large-v3-turbo-q8_0",
            "whisper-large-v3-turbo-q5_0",
            "parakeet-tdt-0.6b-v3-int8",
            "whisper-small-q5_1",
        ]
    };

    for &cid in &candidates {
        let subdir = models_root.join(cid);
        if cid.starts_with("parakeet-tdt") {
            let req = [
                "encoder.int8.onnx",
                "decoder.int8.onnx",
                "joiner.int8.onnx",
                "tokens.txt",
            ];
            if req.iter().all(|f| subdir.join(f).is_file()) {
                return Ok((cid.to_string(), WindowsEngine::SherpaParakeet, subdir));
            }
        } else {
            let bin = subdir.join("ggml-model.bin");
            if bin.is_file() {
                return Ok((cid.to_string(), WindowsEngine::Whisper, bin));
            }
        }
    }

    let missing_name = if !requested_model_id.trim().is_empty() {
        requested_model_id.trim()
    } else {
        "whisper-large-v3-turbo-q8_0 / parakeet-tdt-0.6b-v3-int8"
    };
    Err(SidecarError::ModelMissing(format!(
        "Speech model '{}' is not installed in {}. Download it in Settings -> Audio & Video or via --download-model.",
        missing_name,
        models_root.display()
    )))
}

/// Maps the user's speaker hint to sherpa-onnx clustering parameters:
/// `(num_clusters, threshold)` where `num_clusters == -1` means "decide by
/// threshold".
pub fn parse_speaker_clustering_hint(speakers_hint: &str) -> (i32, f32) {
    let norm = speakers_hint.trim().to_ascii_lowercase();
    if norm.is_empty() || norm == "auto" {
        (-1, 0.50)
    } else if norm == "4+" {
        // A lower agglomerative threshold encourages separating 4+ voices.
        (-1, 0.38)
    } else if let Ok(exact) = norm.parse::<i32>() {
        if exact >= 1 {
            (exact, 0.50)
        } else {
            (-1, 0.50)
        }
    } else {
        (-1, 0.50)
    }
}

// ---------------------------------------------------------------------------
// Shared tuning
// ---------------------------------------------------------------------------

fn optimal_threads() -> i32 {
    let n = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    // ~75% of logical cores, capped: these models stop scaling well past 8
    // threads, and leaving headroom keeps the desktop responsive.
    ((n * 3) / 4).clamp(1, 8) as i32
}

fn diag(msg: &str) {
    // Diagnostics go to stderr; backend/audio.py shows the stderr tail on failure.
    let _ = writeln!(std::io::stderr(), "[docprep-audio] {}", msg);
}

/// Decide whether to run Whisper on the GPU.
///
/// Vulkan has a per-process cost: ggml compiles its compute pipelines when the
/// backend initialises, several seconds on a laptop GPU. For the small models
/// that overhead is larger than the whole CPU transcription, so the GPU is only
/// used for the large models where inference dominates. `DOCPREP_AUDIO_GPU=0|1`
/// overrides the heuristic (support/diagnostics).
fn whisper_gpu_decision(model_id: &str, vulkan: bool, vram_mb: u64) -> (bool, &'static str) {
    match env::var("DOCPREP_AUDIO_GPU").ok().as_deref().map(str::trim) {
        Some("0") | Some("false") | Some("off") => return (false, "forced off by DOCPREP_AUDIO_GPU"),
        Some("1") | Some("true") | Some("on") => return (true, "forced on by DOCPREP_AUDIO_GPU"),
        _ => {}
    }
    if !vulkan {
        return (false, "no Vulkan-capable GPU");
    }
    if vram_mb < 2048 {
        return (false, "GPU has under 2 GB of dedicated memory");
    }
    if !model_id.starts_with("whisper-large") {
        return (false, "small model: CPU avoids per-process Vulkan pipeline compilation");
    }
    (true, "large model on a Vulkan GPU")
}

// ---------------------------------------------------------------------------
// Engine 1: Whisper (whisper-rs, Vulkan GPU with CPU fallback)
// ---------------------------------------------------------------------------

struct AsrOutput {
    words: Vec<WordTiming>,
    text: String,
}

fn open_whisper_context(
    model_bin: &Path,
    use_gpu: bool,
) -> Result<whisper_rs::WhisperContext, whisper_rs::WhisperError> {
    let mut params = whisper_rs::WhisperContextParameters::default();
    params.use_gpu(use_gpu);
    whisper_rs::WhisperContext::new_with_params(model_bin, params)
}

fn run_whisper_asr(
    model_bin: &Path,
    model_id: &str,
    pcm: &[f32],
) -> Result<AsrOutput, SidecarError> {
    // Route ggml/whisper's chatty stderr logging through the `log` facade,
    // which has no subscriber here, so our own diagnostics stay readable.
    whisper_rs::install_logging_hooks();

    let hw = detect_hardware();
    let (want_gpu, why) = whisper_gpu_decision(model_id, hw.vulkan, hw.vram_mb as u64);
    diag(&format!(
        "whisper model={} gpu={} ({})",
        model_id, want_gpu, why
    ));

    let (ctx, on_gpu) = match open_whisper_context(model_bin, want_gpu) {
        Ok(ctx) => (ctx, want_gpu),
        Err(err) if want_gpu => {
            diag(&format!(
                "Vulkan context failed ({}); falling back to CPU",
                err
            ));
            let ctx = open_whisper_context(model_bin, false).map_err(|e| {
                SidecarError::InferenceFailed(format!("Failed to load Whisper model: {}", e))
            })?;
            (ctx, false)
        }
        Err(err) => {
            return Err(SidecarError::InferenceFailed(format!(
                "Failed to load Whisper model: {}",
                err
            )))
        }
    };
    if on_gpu {
        let names: Vec<String> = whisper_rs::vulkan::list_devices()
            .into_iter()
            .map(|d| d.name)
            .collect();
        diag(&format!("Vulkan devices: {}", names.join(" | ")));
    }

    let mut state = ctx.create_state().map_err(|e| {
        SidecarError::InferenceFailed(format!("Failed to create Whisper state: {}", e))
    })?;

    let mut params = whisper_rs::FullParams::new(whisper_rs::SamplingStrategy::Greedy { best_of: 1 });
    params.set_language(Some("auto"));
    params.set_token_timestamps(true);
    params.set_n_threads(optimal_threads());
    params.set_no_context(true);
    params.set_suppress_blank(true);
    params.set_print_special(false);
    params.set_print_progress(false);
    params.set_print_realtime(false);
    params.set_print_timestamps(false);
    params.set_progress_callback_safe(|pct: i32| {
        emit_progress("transcribing", pct.clamp(1, 99));
    });

    state.full(params, pcm).map_err(|e| {
        SidecarError::InferenceFailed(format!("Whisper transcription failed: {}", e))
    })?;

    let lang_id = state.full_lang_id_from_state();
    if let Some(lang) = whisper_rs::get_lang_str(lang_id) {
        diag(&format!("detected language: {}", lang));
    }

    // Collect sub-word tokens with timestamps (whisper reports centiseconds),
    // then merge to words. Token times can be missing (-1) for some tokens;
    // clamp those into their segment's bounds so word order is preserved.
    let mut tokens: Vec<(String, f64, f64)> = Vec::new();
    let mut text = String::new();
    for seg in state.as_iter() {
        let seg_start = seg.start_timestamp() as f64 * 0.01;
        let seg_end = seg.end_timestamp() as f64 * 0.01;
        if let Ok(s) = seg.to_str_lossy() {
            text.push_str(&s);
        }
        let n = seg.n_tokens();
        for i in 0..n {
            let Some(tok) = seg.get_token(i) else { continue };
            let piece = match tok.to_str_lossy() {
                Ok(s) => s.to_string(),
                Err(_) => continue,
            };
            // Special/control tokens: [_BEG_], [_TT_160] (timestamps), [_SOT_],
            // <|en|> etc. They carry no text and would otherwise glue onto words.
            let p = piece.trim();
            if (p.starts_with("[_") && p.ends_with(']')) || (p.starts_with("<|") && p.ends_with("|>")) {
                continue;
            }
            let data = tok.token_data();
            let mut t0 = data.t0 as f64 * 0.01;
            let mut t1 = data.t1 as f64 * 0.01;
            if data.t0 < 0 || t0 < seg_start || t0 > seg_end {
                t0 = seg_start;
            }
            if data.t1 < 0 || t1 < t0 || t1 > seg_end {
                t1 = seg_end.max(t0);
            }
            tokens.push((piece, t0, t1));
        }
    }

    Ok(AsrOutput {
        words: subword_tokens_to_words(&tokens),
        text: text.trim().to_string(),
    })
}

// ---------------------------------------------------------------------------
// Engine 2: Parakeet TDT 0.6B v3 INT8 (sherpa-onnx offline transducer)
// ---------------------------------------------------------------------------

/// Thin owner of a sherpa-onnx offline recognizer. `sherpa_rs::transducer`
/// only exposes the final text; we need token timestamps for diarization
/// alignment, so the recognizer is driven through the C API directly.
struct ParakeetRecognizer {
    ptr: *const sherpa_rs::sherpa_rs_sys::SherpaOnnxOfflineRecognizer,
    // Keep the C strings alive for the recognizer's lifetime.
    _strings: Vec<CString>,
}

struct TokenizedResult {
    text: String,
    tokens: Vec<String>,
    timestamps: Vec<f32>,
}

impl ParakeetRecognizer {
    fn new(model_dir: &Path) -> Result<Self, SidecarError> {
        use sherpa_rs::sherpa_rs_sys as sys;

        let cs = |p: PathBuf| -> Result<CString, SidecarError> {
            CString::new(p.to_string_lossy().as_bytes()).map_err(|_| {
                SidecarError::ModelMissing(format!("Invalid model path: {}", p.display()))
            })
        };
        let encoder = cs(model_dir.join("encoder.int8.onnx"))?;
        let decoder = cs(model_dir.join("decoder.int8.onnx"))?;
        let joiner = cs(model_dir.join("joiner.int8.onnx"))?;
        let tokens = cs(model_dir.join("tokens.txt"))?;
        let provider = CString::new("cpu").unwrap();
        let model_type = CString::new("nemo_transducer").unwrap();
        let decoding = CString::new("greedy_search").unwrap();
        let empty = CString::new("").unwrap();

        let ptr = unsafe {
            let mut model_config: sys::SherpaOnnxOfflineModelConfig = mem::zeroed();
            model_config.transducer = sys::SherpaOnnxOfflineTransducerModelConfig {
                encoder: encoder.as_ptr(),
                decoder: decoder.as_ptr(),
                joiner: joiner.as_ptr(),
            };
            model_config.tokens = tokens.as_ptr();
            model_config.num_threads = optimal_threads();
            model_config.provider = provider.as_ptr();
            model_config.model_type = model_type.as_ptr();
            model_config.modeling_unit = empty.as_ptr();
            model_config.bpe_vocab = empty.as_ptr();
            model_config.debug = 0;

            let mut config: sys::SherpaOnnxOfflineRecognizerConfig = mem::zeroed();
            config.model_config = model_config;
            config.feat_config = sys::SherpaOnnxFeatureConfig {
                sample_rate: 16000,
                feature_dim: 80,
            };
            config.decoding_method = decoding.as_ptr();
            config.hotwords_file = empty.as_ptr();
            config.hotwords_score = 1.5;
            config.blank_penalty = 0.0;
            config.max_active_paths = 4;

            sys::SherpaOnnxCreateOfflineRecognizer(&config)
        };
        if ptr.is_null() {
            return Err(SidecarError::InferenceFailed(format!(
                "sherpa-onnx could not load the Parakeet model from {}",
                model_dir.display()
            )));
        }
        Ok(Self {
            ptr,
            _strings: vec![encoder, decoder, joiner, tokens, provider, model_type, decoding, empty],
        })
    }

    fn recognize(&self, samples: &[f32]) -> TokenizedResult {
        use sherpa_rs::sherpa_rs_sys as sys;
        unsafe {
            let stream = sys::SherpaOnnxCreateOfflineStream(self.ptr);
            sys::SherpaOnnxAcceptWaveformOffline(
                stream,
                16000,
                samples.as_ptr(),
                samples.len() as i32,
            );
            sys::SherpaOnnxDecodeOfflineStream(self.ptr, stream);
            let result_ptr = sys::SherpaOnnxGetOfflineStreamResult(stream);
            let out = if result_ptr.is_null() {
                TokenizedResult { text: String::new(), tokens: Vec::new(), timestamps: Vec::new() }
            } else {
                let raw = &*result_ptr;
                let text = if raw.text.is_null() {
                    String::new()
                } else {
                    CStr::from_ptr(raw.text).to_string_lossy().into_owned()
                };
                let count = raw.count.max(0) as usize;
                let timestamps = if raw.timestamps.is_null() {
                    Vec::new()
                } else {
                    std::slice::from_raw_parts(raw.timestamps, count).to_vec()
                };
                // `tokens` is `count` NUL-terminated strings laid out back to back.
                let mut tokens = Vec::with_capacity(count);
                if !raw.tokens.is_null() {
                    let mut p = raw.tokens;
                    for _ in 0..count {
                        let c = CStr::from_ptr(p);
                        tokens.push(c.to_string_lossy().into_owned());
                        p = p.add(c.to_bytes_with_nul().len());
                    }
                }
                sys::SherpaOnnxDestroyOfflineRecognizerResult(result_ptr);
                TokenizedResult { text, tokens, timestamps }
            };
            sys::SherpaOnnxDestroyOfflineStream(stream);
            out
        }
    }
}

impl Drop for ParakeetRecognizer {
    fn drop(&mut self) {
        unsafe { sherpa_rs::sherpa_rs_sys::SherpaOnnxDestroyOfflineRecognizer(self.ptr) };
    }
}

fn run_parakeet_asr(model_dir: &Path, pcm: &[f32]) -> Result<AsrOutput, SidecarError> {
    diag("parakeet-tdt-0.6b-v3 int8 on CPU (sherpa-onnx)");
    let recognizer = ParakeetRecognizer::new(model_dir)?;

    // Bounded memory and smooth progress: 30 s windows, timestamps re-based to
    // the window's offset. The transducer is streaming-friendly, so boundary
    // effects are a word at most.
    const CHUNK: usize = 30 * 16000;
    let total = pcm.len();
    let mut tokens: Vec<(String, f64, f64)> = Vec::new();
    let mut text_parts: Vec<String> = Vec::new();
    let mut offset = 0usize;
    while offset < total {
        let end = (offset + CHUNK).min(total);
        let base = offset as f64 / 16000.0;
        let res = recognizer.recognize(&pcm[offset..end]);
        let n = res.tokens.len().min(res.timestamps.len());
        for i in 0..n {
            let start = base + res.timestamps[i] as f64;
            let next = if i + 1 < n {
                base + res.timestamps[i + 1] as f64
            } else {
                (end as f64 / 16000.0).max(start + 0.08)
            };
            tokens.push((res.tokens[i].clone(), start, next.max(start)));
        }
        if !res.text.trim().is_empty() {
            text_parts.push(res.text.trim().to_string());
        }
        offset = end;
        let pct = ((offset as f64 / total as f64) * 100.0) as i32;
        emit_progress("transcribing", pct.clamp(1, 99));
    }

    Ok(AsrOutput {
        words: subword_tokens_to_words(&tokens),
        text: text_parts.join(" "),
    })
}

// ---------------------------------------------------------------------------
// Speaker diarization (sherpa-onnx: pyannote segmentation + CAM++ embeddings)
// ---------------------------------------------------------------------------

fn run_sherpa_diarization(
    models_root: &Path,
    pcm: &[f32],
    speakers_hint: &str,
) -> Result<Vec<TimedSpeakerSegment>, SidecarError> {
    use sherpa_rs::diarize::{Diarize, DiarizeConfig};

    let seg_model = models_root.join("diarization").join("segmentation.int8.onnx");
    let emb_model = models_root.join("diarization").join("embedding.onnx");
    let (num_clusters, threshold) = parse_speaker_clustering_hint(speakers_hint);
    diag(&format!(
        "diarization clusters={} threshold={:.2}",
        num_clusters, threshold
    ));

    let config = DiarizeConfig {
        num_clusters: Some(num_clusters),
        threshold: Some(threshold),
        // Drop sub-300 ms speaker blips and bridge sub-500 ms gaps: keeps
        // turns from fragmenting on breaths and overlaps.
        min_duration_on: Some(0.3),
        min_duration_off: Some(0.5),
        provider: Some("cpu".to_string()),
        debug: false,
    };
    let mut diarizer = Diarize::new(&seg_model, &emb_model, config).map_err(|e| {
        SidecarError::InferenceFailed(format!("Failed to load diarization models: {}", e))
    })?;

    let segments = diarizer
        .compute(
            pcm.to_vec(),
            Some(Box::new(|processed: i32, total: i32| {
                if total > 0 {
                    let pct = ((processed as f64 / total as f64) * 100.0) as i32;
                    emit_progress("diarizing", pct.clamp(1, 99));
                }
                0
            })),
        )
        .map_err(|e| SidecarError::InferenceFailed(format!("Speaker diarization failed: {}", e)))?;

    Ok(segments
        .into_iter()
        .map(|s| TimedSpeakerSegment {
            speaker_id: format!("speaker_{:02}", s.speaker.max(0)),
            start: s.start as f64,
            end: s.end as f64,
        })
        .collect())
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

pub fn run_transcribe(
    file_path: &str,
    models_root_str: &str,
    model_id_arg: &str,
    diarize: bool,
    speakers_hint: &str,
) -> Result<(), SidecarError> {
    let input_path = Path::new(file_path);
    if !input_path.is_file() {
        return Err(SidecarError::FileNotFound(format!(
            "Audio/video file not found: {}",
            file_path
        )));
    }

    let models_root = Path::new(models_root_str);
    let (resolved_id, engine, model_path) = resolve_model_and_engine(models_root, model_id_arg)?;

    if diarize {
        let seg_model = models_root.join("diarization").join("segmentation.int8.onnx");
        let emb_model = models_root.join("diarization").join("embedding.onnx");
        if !seg_model.is_file() || !emb_model.is_file() {
            return Err(SidecarError::ModelMissing(format!(
                "Speaker diarization weights are missing in {}. Re-download your speech model or pass --no-diarize.",
                models_root.join("diarization").display()
            )));
        }
    }

    emit_progress("decoding", 0);
    let norm_wav = prepare_normalized_wav(input_path)?;
    if norm_wav.sample_count == 0 || norm_wav.duration_sec <= 0.01 {
        emit_result(0.0, 0, &[]);
        return Ok(());
    }
    let pcm = load_normalized_wav_f32(&norm_wav.wav_path)?;
    emit_progress("decoding", 100);

    emit_progress("transcribing", 0);
    let asr = match engine {
        WindowsEngine::Whisper => run_whisper_asr(&model_path, &resolved_id, &pcm)?,
        WindowsEngine::SherpaParakeet => run_parakeet_asr(&model_path, &pcm)?,
    };
    emit_progress("transcribing", 100);

    if !diarize {
        let segments = build_undiarized_segments(&asr.words, &asr.text, norm_wav.duration_sec);
        emit_result(round_millis(norm_wav.duration_sec), 0, &segments);
        return Ok(());
    }

    emit_progress("diarizing", 0);
    let speaker_segments = run_sherpa_diarization(models_root, &pcm, speakers_hint)?;
    emit_progress("diarizing", 100);

    let (segments, speaker_count) = build_diarized_segments(
        &asr.words,
        &asr.text,
        norm_wav.duration_sec,
        &speaker_segments,
    );
    emit_result(round_millis(norm_wav.duration_sec), speaker_count, &segments);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gpu_only_for_large_models_on_capable_gpus() {
        env::remove_var("DOCPREP_AUDIO_GPU");
        assert!(whisper_gpu_decision("whisper-large-v3-turbo-q8_0", true, 6001).0);
        assert!(!whisper_gpu_decision("whisper-small-q5_1", true, 6001).0);
        assert!(!whisper_gpu_decision("whisper-large-v3-turbo-q8_0", false, 6001).0);
        assert!(!whisper_gpu_decision("whisper-large-v3-turbo-q8_0", true, 1024).0);
    }

    #[test]
    fn speaker_hint_mapping() {
        assert_eq!(parse_speaker_clustering_hint("auto"), (-1, 0.50));
        assert_eq!(parse_speaker_clustering_hint("2"), (2, 0.50));
        assert_eq!(parse_speaker_clustering_hint("4+"), (-1, 0.38));
        assert_eq!(parse_speaker_clustering_hint("garbage"), (-1, 0.50));
    }

    #[test]
    fn resolve_prefers_installed_model_in_priority_order() {
        let root = env::temp_dir().join(format!("docprep-resolve-{}", std::process::id()));
        let small = root.join("whisper-small-q5_1");
        std::fs::create_dir_all(&small).unwrap();
        std::fs::write(small.join("ggml-model.bin"), b"x").unwrap();
        let (id, engine, path) = resolve_model_and_engine(&root, "").unwrap();
        assert_eq!(id, "whisper-small-q5_1");
        assert_eq!(engine, WindowsEngine::Whisper);
        assert!(path.ends_with("ggml-model.bin"));
        let _ = std::fs::remove_dir_all(&root);
    }
}
