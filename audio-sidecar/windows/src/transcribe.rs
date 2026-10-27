use std::env;
use std::ffi::{c_char, c_float, c_int, c_void, CStr, CString};
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};

use crate::align::{
    build_diarized_segments, build_undiarized_segments, interpolate_segment_words, round_millis,
    subword_tokens_to_words, TimedSpeakerSegment, WordTiming,
};
use crate::decode::{load_normalized_wav_f32, prepare_normalized_wav, WavStreamWriter};
use crate::hardware::detect_hardware;
use crate::json::{emit_progress, emit_result, parse_json};
use crate::SidecarError;

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
            dirs.push(exe_dir.join("_internal").join("audio_runtime"));
            dirs.push(exe_dir.join("audio_runtime"));
            dirs.push(exe_dir.to_path_buf());
            if let Some(parent) = exe_dir.parent() {
                dirs.push(parent.join("Resources").join("_internal").join("audio_runtime"));
                dirs.push(parent.join("_internal").join("audio_runtime"));
            }
        }
    }

    // Repo dev locations
    if let Ok(cwd) = env::current_dir() {
        dirs.push(
            cwd.join("desktop")
                .join("src-tauri")
                .join("binaries")
                .join("_internal")
                .join("audio_runtime"),
        );
        dirs.push(
            cwd.join("audio-sidecar")
                .join("windows")
                .join(".vendor_cache")
                .join("runtime"),
        );
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

    #[cfg(not(target_os = "windows"))]
    {
        for sys_dir in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"] {
            let p = Path::new(sys_dir).join(&file_name);
            if p.is_file() {
                return Some(p);
            }
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

pub fn find_runtime_library(lib_stem: &str) -> Option<PathBuf> {
    let names: Vec<String> = if cfg!(target_os = "windows") {
        vec![format!("{}.dll", lib_stem)]
    } else if cfg!(target_os = "macos") {
        vec![
            format!("lib{}.dylib", lib_stem),
            format!("{}.dylib", lib_stem),
        ]
    } else {
        vec![format!("lib{}.so", lib_stem), format!("{}.so", lib_stem)]
    };

    for dir in runtime_search_dirs() {
        for name in &names {
            let candidate = dir.join(name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

fn prepend_runtime_to_path_env(cmd: &mut Command) {
    let sep = if cfg!(target_os = "windows") { ";" } else { ":" };
    let mut path_parts: Vec<String> = Vec::new();
    for d in runtime_search_dirs() {
        if d.is_dir() {
            path_parts.push(d.to_string_lossy().into_owned());
        }
    }
    if let Ok(existing) = env::var("PATH") {
        path_parts.push(existing);
    }
    cmd.env("PATH", path_parts.join(sep));
}

fn optimal_threads() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get().clamp(1, 8))
        .unwrap_or(4)
}

// MARK: - Cross-Platform Dynamic Library Loader (`LoadLibraryExW` / `dlopen`)

struct DynLib {
    handle: *mut c_void,
}

#[cfg(target_os = "windows")]
mod dyn_os {
    use std::ffi::c_void;
    use std::os::windows::ffi::OsStrExt;
    use std::path::Path;
    use std::ptr::null_mut;

    const LOAD_WITH_ALTERED_SEARCH_PATH: u32 = 0x0000_0008;

    #[link(name = "kernel32")]
    extern "system" {
        fn LoadLibraryExW(
            lp_lib_file_name: *const u16,
            h_file: *mut c_void,
            dw_flags: u32,
        ) -> *mut c_void;
        fn GetProcAddress(h_module: *mut c_void, lp_proc_name: *const u8) -> *mut c_void;
        fn FreeLibrary(h_lib_module: *mut c_void) -> i32;
        fn SetDllDirectoryW(lp_path_name: *const u16) -> i32;
    }

    pub unsafe fn open_lib(path: &Path) -> Option<*mut c_void> {
        if let Some(parent) = path.parent() {
            let dir_wide: Vec<u16> = parent
                .as_os_str()
                .encode_wide()
                .chain(std::iter::once(0))
                .collect();
            let _ = SetDllDirectoryW(dir_wide.as_ptr());
        }
        let wide: Vec<u16> = path
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let h = LoadLibraryExW(wide.as_ptr(), null_mut(), LOAD_WITH_ALTERED_SEARCH_PATH);
        if h.is_null() {
            None
        } else {
            Some(h)
        }
    }

    pub unsafe fn get_sym(handle: *mut c_void, name_cstr: &[u8]) -> Option<*mut c_void> {
        let p = GetProcAddress(handle, name_cstr.as_ptr());
        if p.is_null() {
            None
        } else {
            Some(p)
        }
    }

    pub unsafe fn close_lib(handle: *mut c_void) {
        if !handle.is_null() {
            let _ = FreeLibrary(handle);
        }
    }
}

#[cfg(not(target_os = "windows"))]
mod dyn_os {
    use std::ffi::{c_char, c_int, c_void, CString};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;

    const RTLD_NOW: c_int = 2;

    extern "C" {
        fn dlopen(filename: *const c_char, flag: c_int) -> *mut c_void;
        fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
        fn dlclose(handle: *mut c_void) -> c_int;
    }

    pub unsafe fn open_lib(path: &Path) -> Option<*mut c_void> {
        let c_path = CString::new(path.as_os_str().as_bytes()).ok()?;
        let h = dlopen(c_path.as_ptr(), RTLD_NOW);
        if h.is_null() {
            None
        } else {
            Some(h)
        }
    }

    pub unsafe fn get_sym(handle: *mut c_void, name_cstr: &[u8]) -> Option<*mut c_void> {
        let p = dlsym(handle, name_cstr.as_ptr() as *const c_char);
        if p.is_null() {
            None
        } else {
            Some(p)
        }
    }

    pub unsafe fn close_lib(handle: *mut c_void) {
        if !handle.is_null() {
            let _ = dlclose(handle);
        }
    }
}

impl DynLib {
    fn open(path: &Path) -> Option<Self> {
        unsafe { dyn_os::open_lib(path).map(|handle| Self { handle }) }
    }

    unsafe fn sym<T>(&self, name_with_nul: &[u8]) -> Option<T> {
        let ptr = dyn_os::get_sym(self.handle, name_with_nul)?;
        Some(std::mem::transmute_copy(&ptr))
    }
}

impl Drop for DynLib {
    fn drop(&mut self) {
        unsafe {
            dyn_os::close_lib(self.handle);
        }
    }
}

// MARK: - Model Resolution & Validation

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

// MARK: - Engine 1: Whisper (Vulkan GPU + CPU AVX2 fallback)

fn parse_whisper_json_output(json_text: &str) -> Result<(Vec<WordTiming>, String), SidecarError> {
    let root = parse_json(json_text).map_err(|e| {
        SidecarError::InferenceFailed(format!("Failed to parse Whisper JSON output: {}", e))
    })?;

    let mut all_words: Vec<WordTiming> = Vec::new();
    let mut full_text = String::new();

    if let Some(segments) = root.get("transcription").and_then(|v| v.as_array()) {
        for seg in segments {
            let seg_text = seg.get("text").and_then(|v| v.as_str()).unwrap_or("");
            if !seg_text.trim().is_empty() {
                if !full_text.is_empty() {
                    full_text.push(' ');
                }
                full_text.push_str(seg_text.trim());
            }

            let seg_from_ms = seg
                .get("offsets")
                .and_then(|o| o.get("from"))
                .and_then(|v| v.as_f64())
                .unwrap_or(0.0);
            let seg_to_ms = seg
                .get("offsets")
                .and_then(|o| o.get("to"))
                .and_then(|v| v.as_f64())
                .unwrap_or(seg_from_ms);
            let seg_start = (seg_from_ms / 1000.0).max(0.0);
            let seg_end = (seg_to_ms / 1000.0).max(seg_start);

            let mut seg_tokens: Vec<(String, f64, f64)> = Vec::new();
            if let Some(tokens) = seg.get("tokens").and_then(|v| v.as_array()) {
                for tok in tokens {
                    let t_str = tok.get("text").and_then(|v| v.as_str()).unwrap_or("");
                    if t_str.is_empty() {
                        continue;
                    }
                    let t_from = tok
                        .get("offsets")
                        .and_then(|o| o.get("from"))
                        .and_then(|v| v.as_f64())
                        .map(|ms| ms / 1000.0)
                        .unwrap_or(seg_start);
                    let t_to = tok
                        .get("offsets")
                        .and_then(|o| o.get("to"))
                        .and_then(|v| v.as_f64())
                        .map(|ms| ms / 1000.0)
                        .unwrap_or(t_from);
                    seg_tokens.push((t_str.to_string(), t_from, t_to.max(t_from)));
                }
            }

            let words = if !seg_tokens.is_empty() {
                subword_tokens_to_words(&seg_tokens)
            } else {
                interpolate_segment_words(seg_text, seg_start, seg_end)
            };
            all_words.extend(words);
        }
    }

    Ok((all_words, full_text))
}

fn find_whisper_cli() -> Option<PathBuf> {
    for name in ["whisper-cli", "whisper", "main"] {
        if let Some(p) = find_runtime_or_system_exe(name) {
            return Some(p);
        }
    }
    None
}

fn invoke_whisper_cli_once(
    cli_path: &Path,
    model_bin: &Path,
    wav_path: &Path,
    use_gpu: bool,
) -> Result<(Vec<WordTiming>, String), String> {
    let out_prefix = env::temp_dir().join(format!(
        "docprep-whisper-out-{}",
        std::process::id()
    ));
    let out_json = PathBuf::from(format!("{}.json", out_prefix.display()));
    let _ = fs::remove_file(&out_json);

    let threads = optimal_threads().to_string();
    let mut cmd = Command::new(cli_path);
    cmd.args([
        "-m",
        &model_bin.to_string_lossy(),
        "-f",
        &wav_path.to_string_lossy(),
        "-l",
        "auto",
        "-t",
        &threads,
        "-ojf",
        "-of",
        &out_prefix.to_string_lossy(),
        "-pp",
    ]);
    if !use_gpu {
        cmd.arg("-ng");
    }
    prepend_runtime_to_path_env(&mut cmd);
    apply_no_window(&mut cmd);
    cmd.stdout(Stdio::null());
    cmd.stderr(Stdio::piped());

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Failed to start whisper-cli: {}", e))?;

    let mut last_pct = 0i32;
    let mut stderr_tail = Vec::new();
    if let Some(stderr) = child.stderr.take() {
        let reader = BufReader::new(stderr);
        for line in reader.lines().flatten() {
            // Look for "progress =  45%"
            if let Some(idx) = line.find("progress =") {
                let rest = &line[idx + "progress =".len()..];
                let digits: String = rest
                    .chars()
                    .skip_while(|c| c.is_whitespace())
                    .take_while(|c| c.is_ascii_digit())
                    .collect();
                if let Ok(pct) = digits.parse::<i32>() {
                    let clamped = pct.clamp(1, 99);
                    if clamped >= last_pct + 2 {
                        last_pct = clamped;
                        emit_progress("transcribing", clamped);
                    }
                }
            } else if !line.trim().is_empty() {
                if stderr_tail.len() >= 12 {
                    stderr_tail.remove(0);
                }
                stderr_tail.push(line);
            }
        }
    }

    let status = child
        .wait()
        .map_err(|e| format!("Failed waiting for whisper-cli: {}", e))?;

    if !status.success() || !out_json.is_file() {
        let _ = fs::remove_file(&out_json);
        return Err(stderr_tail.join(" | "));
    }

    let json_text = fs::read_to_string(&out_json)
        .map_err(|e| format!("Failed reading Whisper JSON output: {}", e))?;
    let _ = fs::remove_file(&out_json);

    parse_whisper_json_output(&json_text).map_err(|e| e.message().to_string())
}

fn run_whisper_asr(model_bin: &Path, wav_path: &Path) -> Result<(Vec<WordTiming>, String), SidecarError> {
    let cli_path = find_whisper_cli().ok_or_else(|| {
        SidecarError::InferenceFailed(
            "Whisper runtime binary ('whisper-cli.exe') was not found in _internal/audio_runtime or PATH. Re-run desktop/build-sidecar.ps1.".to_string(),
        )
    })?;

    let hw = detect_hardware();
    let try_gpu = hw.vulkan && hw.vram_mb > 0;

    if try_gpu {
        match invoke_whisper_cli_once(&cli_path, model_bin, wav_path, true) {
            Ok(res) => return Ok(res),
            Err(_) => {
                // Automatic CPU fallback if Vulkan device allocation fails
            }
        }
    }

    invoke_whisper_cli_once(&cli_path, model_bin, wav_path, false).map_err(|err| {
        SidecarError::InferenceFailed(format!("Whisper transcription failed: {}", err))
    })
}

// MARK: - Engine 2: Sherpa-ONNX Parakeet TDT 0.6B v3 INT8 (C-API DLL + CLI fallback)

// Exact C ABI structs for sherpa-onnx v1.12.x (`c-api/c-api.h`)
#[repr(C)]
struct SherpaOnnxFeatureConfig {
    sample_rate: c_int,
    feature_dim: c_int,
}

#[repr(C)]
struct SherpaOnnxOfflineTransducerModelConfig {
    encoder: *const c_char,
    decoder: *const c_char,
    joiner: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineParaformerModelConfig {
    model: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineNemoEncDecCtcModelConfig {
    model: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineWhisperModelConfig {
    encoder: *const c_char,
    decoder: *const c_char,
    language: *const c_char,
    task: *const c_char,
    tail_paddings: c_int,
}

#[repr(C)]
struct SherpaOnnxOfflineTdnnModelConfig {
    model: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineSenseVoiceModelConfig {
    model: *const c_char,
    language: *const c_char,
    use_itn: c_int,
}

#[repr(C)]
struct SherpaOnnxOfflineMoonshineModelConfig {
    preprocessor: *const c_char,
    encoder: *const c_char,
    uncached_decoder: *const c_char,
    cached_decoder: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineFireRedAsrModelConfig {
    encoder: *const c_char,
    decoder: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineDolphinModelConfig {
    model: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineZipformerCtcModelConfig {
    model: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineCanaryModelConfig {
    encoder: *const c_char,
    decoder: *const c_char,
    src_lang: *const c_char,
    tgt_lang: *const c_char,
    use_pnc: c_int,
}

#[repr(C)]
struct SherpaOnnxOfflineWenetCtcModelConfig {
    model: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineOmnilingualAsrCtcModelConfig {
    model: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineMedAsrCtcModelConfig {
    model: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineFunAsrNanoModelConfig {
    encoder_adaptor: *const c_char,
    llm: *const c_char,
    embedding: *const c_char,
    tokenizer: *const c_char,
    system_prompt: *const c_char,
    user_prompt: *const c_char,
    max_new_tokens: c_int,
    temperature: c_float,
    top_p: c_float,
    seed: c_int,
    language: *const c_char,
    itn: c_int,
    hotwords: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineModelConfig {
    transducer: SherpaOnnxOfflineTransducerModelConfig,
    paraformer: SherpaOnnxOfflineParaformerModelConfig,
    nemo_ctc: SherpaOnnxOfflineNemoEncDecCtcModelConfig,
    whisper: SherpaOnnxOfflineWhisperModelConfig,
    tdnn: SherpaOnnxOfflineTdnnModelConfig,
    tokens: *const c_char,
    num_threads: c_int,
    debug: c_int,
    provider: *const c_char,
    model_type: *const c_char,
    modeling_unit: *const c_char,
    bpe_vocab: *const c_char,
    telespeech_ctc: *const c_char,
    sense_voice: SherpaOnnxOfflineSenseVoiceModelConfig,
    moonshine: SherpaOnnxOfflineMoonshineModelConfig,
    fire_red_asr: SherpaOnnxOfflineFireRedAsrModelConfig,
    dolphin: SherpaOnnxOfflineDolphinModelConfig,
    zipformer_ctc: SherpaOnnxOfflineZipformerCtcModelConfig,
    canary: SherpaOnnxOfflineCanaryModelConfig,
    wenet_ctc: SherpaOnnxOfflineWenetCtcModelConfig,
    omnilingual: SherpaOnnxOfflineOmnilingualAsrCtcModelConfig,
    medasr: SherpaOnnxOfflineMedAsrCtcModelConfig,
    funasr_nano: SherpaOnnxOfflineFunAsrNanoModelConfig,
}

#[repr(C)]
struct SherpaOnnxOfflineLMConfig {
    model: *const c_char,
    scale: c_float,
}

#[repr(C)]
struct SherpaOnnxHomophoneReplacerConfig {
    dict_dir: *const c_char,
    lexicon: *const c_char,
    rule_fsts: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineRecognizerConfig {
    feat_config: SherpaOnnxFeatureConfig,
    model_config: SherpaOnnxOfflineModelConfig,
    lm_config: SherpaOnnxOfflineLMConfig,
    decoding_method: *const c_char,
    max_active_paths: c_int,
    hotwords_file: *const c_char,
    hotwords_score: c_float,
    rule_fsts: *const c_char,
    rule_far: *const c_char,
    blank_penalty: c_float,
    hr: SherpaOnnxHomophoneReplacerConfig,
}

pub fn parse_sherpa_result_json(
    json_str: &str,
    window_offset_sec: f64,
    window_duration_sec: f64,
) -> (Vec<WordTiming>, String) {
    let parsed = match parse_json(json_str) {
        Ok(v) => v,
        Err(_) => return (Vec::new(), String::new()),
    };

    let text = parsed
        .get("text")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();

    let tokens_arr = parsed.get("tokens").and_then(|v| v.as_array());
    let ts_arr = parsed.get("timestamps").and_then(|v| v.as_array());

    if let (Some(toks), Some(ts)) = (tokens_arr, ts_arr) {
        if !toks.is_empty() && toks.len() == ts.len() {
            let mut subwords = Vec::with_capacity(toks.len());
            for i in 0..toks.len() {
                let tok_str = toks[i].as_str().unwrap_or("").to_string();
                let t0 = window_offset_sec + ts[i].as_f64().unwrap_or(0.0).max(0.0);
                let t1 = if i + 1 < ts.len() {
                    let next_t = window_offset_sec + ts[i + 1].as_f64().unwrap_or(0.0);
                    next_t.max(t0 + 0.04).min(t0 + 0.60)
                } else {
                    (t0 + 0.18).min(window_offset_sec + window_duration_sec)
                };
                subwords.push((tok_str, t0, t1.max(t0)));
            }
            let words = subword_tokens_to_words(&subwords);
            if !words.is_empty() {
                return (words, text);
            }
        }
    }

    let words = interpolate_segment_words(
        &text,
        window_offset_sec,
        window_offset_sec + window_duration_sec,
    );
    (words, text)
}

fn run_parakeet_via_c_api(
    model_dir: &Path,
    wav_path: &Path,
) -> Result<(Vec<WordTiming>, String), String> {
    let dll_path = find_runtime_library("sherpa-onnx-c-api")
        .ok_or_else(|| "sherpa-onnx-c-api shared library not found".to_string())?;
    let lib = DynLib::open(&dll_path)
        .ok_or_else(|| format!("Failed to load {}", dll_path.display()))?;

    type CreateRecognizerFn =
        unsafe extern "C" fn(*const SherpaOnnxOfflineRecognizerConfig) -> *const c_void;
    type DestroyRecognizerFn = unsafe extern "C" fn(*const c_void);
    type CreateStreamFn = unsafe extern "C" fn(*const c_void) -> *const c_void;
    type DestroyStreamFn = unsafe extern "C" fn(*const c_void);
    type AcceptWaveformFn =
        unsafe extern "C" fn(*const c_void, c_int, *const c_float, c_int);
    type DecodeStreamFn = unsafe extern "C" fn(*const c_void, *const c_void);
    type GetResultJsonFn = unsafe extern "C" fn(*const c_void) -> *const c_char;
    type DestroyResultJsonFn = unsafe extern "C" fn(*const c_char);

    unsafe {
        let create_rec: CreateRecognizerFn = lib
            .sym(b"SherpaOnnxCreateOfflineRecognizer\0")
            .ok_or("Missing symbol SherpaOnnxCreateOfflineRecognizer")?;
        let destroy_rec: DestroyRecognizerFn = lib
            .sym(b"SherpaOnnxDestroyOfflineRecognizer\0")
            .ok_or("Missing symbol SherpaOnnxDestroyOfflineRecognizer")?;
        let create_stream: CreateStreamFn = lib
            .sym(b"SherpaOnnxCreateOfflineStream\0")
            .ok_or("Missing symbol SherpaOnnxCreateOfflineStream")?;
        let destroy_stream: DestroyStreamFn = lib
            .sym(b"SherpaOnnxDestroyOfflineStream\0")
            .ok_or("Missing symbol SherpaOnnxDestroyOfflineStream")?;
        let accept_wave: AcceptWaveformFn = lib
            .sym(b"SherpaOnnxAcceptWaveformOffline\0")
            .ok_or("Missing symbol SherpaOnnxAcceptWaveformOffline")?;
        let decode_stream: DecodeStreamFn = lib
            .sym(b"SherpaOnnxDecodeOfflineStream\0")
            .ok_or("Missing symbol SherpaOnnxDecodeOfflineStream")?;
        let get_json: GetResultJsonFn = lib
            .sym(b"SherpaOnnxGetOfflineStreamResultAsJson\0")
            .ok_or("Missing symbol SherpaOnnxGetOfflineStreamResultAsJson")?;
        let destroy_json: DestroyResultJsonFn = lib
            .sym(b"SherpaOnnxDestroyOfflineStreamResultJson\0")
            .ok_or("Missing symbol SherpaOnnxDestroyOfflineStreamResultJson")?;

        let encoder_c = CString::new(model_dir.join("encoder.int8.onnx").to_string_lossy().as_bytes())
            .map_err(|e| e.to_string())?;
        let decoder_c = CString::new(model_dir.join("decoder.int8.onnx").to_string_lossy().as_bytes())
            .map_err(|e| e.to_string())?;
        let joiner_c = CString::new(model_dir.join("joiner.int8.onnx").to_string_lossy().as_bytes())
            .map_err(|e| e.to_string())?;
        let tokens_c = CString::new(model_dir.join("tokens.txt").to_string_lossy().as_bytes())
            .map_err(|e| e.to_string())?;
        let provider_c = CString::new("cpu").unwrap();
        let model_type_c = CString::new("nemo_transducer").unwrap();
        let decoding_c = CString::new("greedy_search").unwrap();

        let mut cfg: SherpaOnnxOfflineRecognizerConfig = std::mem::zeroed();
        cfg.feat_config.sample_rate = 16000;
        cfg.feat_config.feature_dim = 80;
        cfg.model_config.transducer.encoder = encoder_c.as_ptr();
        cfg.model_config.transducer.decoder = decoder_c.as_ptr();
        cfg.model_config.transducer.joiner = joiner_c.as_ptr();
        cfg.model_config.tokens = tokens_c.as_ptr();
        cfg.model_config.num_threads = optimal_threads() as c_int;
        cfg.model_config.debug = 0;
        cfg.model_config.provider = provider_c.as_ptr();
        cfg.model_config.model_type = model_type_c.as_ptr();
        cfg.decoding_method = decoding_c.as_ptr();

        let recognizer = create_rec(&cfg);
        if recognizer.is_null() {
            return Err("SherpaOnnxCreateOfflineRecognizer returned null".to_string());
        }

        let samples = load_normalized_wav_f32(wav_path).map_err(|e| e.message().to_string())?;
        let chunk_samples = 30 * 16000; // 30s chunks for smooth progress reporting
        let total_samples = samples.len().max(1);

        let mut all_words: Vec<WordTiming> = Vec::new();
        let mut full_text = String::new();
        let mut offset = 0usize;

        while offset < samples.len() {
            let end = (offset + chunk_samples).min(samples.len());
            let slice = &samples[offset..end];
            let offset_sec = (offset as f64) / 16000.0;
            let dur_sec = (slice.len() as f64) / 16000.0;

            let stream = create_stream(recognizer);
            if !stream.is_null() {
                accept_wave(
                    stream,
                    16000,
                    slice.as_ptr(),
                    slice.len() as c_int,
                );
                decode_stream(recognizer, stream);
                let json_ptr = get_json(stream);
                if !json_ptr.is_null() {
                    let json_str = CStr::from_ptr(json_ptr).to_string_lossy();
                    let (w, t) = parse_sherpa_result_json(&json_str, offset_sec, dur_sec);
                    all_words.extend(w);
                    if !t.is_empty() {
                        if !full_text.is_empty() {
                            full_text.push(' ');
                        }
                        full_text.push_str(&t);
                    }
                    destroy_json(json_ptr);
                }
                destroy_stream(stream);
            }

            offset = end;
            let pct = (((offset as f64) / (total_samples as f64)) * 99.0).round() as i32;
            emit_progress("transcribing", pct.clamp(1, 99));
        }

        destroy_rec(recognizer);
        Ok((all_words, full_text))
    }
}

fn run_parakeet_via_cli(
    model_dir: &Path,
    wav_path: &Path,
) -> Result<(Vec<WordTiming>, String), SidecarError> {
    let exe = find_runtime_or_system_exe("sherpa-onnx-offline").ok_or_else(|| {
        SidecarError::InferenceFailed(
            "Sherpa-ONNX runtime ('sherpa-onnx-c-api.dll' / 'sherpa-onnx-offline.exe') was not found in _internal/audio_runtime. Re-run desktop/build-sidecar.ps1.".to_string(),
        )
    })?;

    let samples = load_normalized_wav_f32(wav_path)?;
    let chunk_samples = 30 * 16000;
    let total_samples = samples.len().max(1);
    let mut all_words: Vec<WordTiming> = Vec::new();
    let mut full_text = String::new();
    let mut offset = 0usize;
    let mut chunk_idx = 0usize;

    while offset < samples.len() {
        let end = (offset + chunk_samples).min(samples.len());
        let slice = &samples[offset..end];
        let offset_sec = (offset as f64) / 16000.0;
        let dur_sec = (slice.len() as f64) / 16000.0;

        let chunk_wav = env::temp_dir().join(format!(
            "docprep-parakeet-chunk-{}-{}.wav",
            std::process::id(),
            chunk_idx
        ));
        chunk_idx += 1;

        let mut writer = WavStreamWriter::create(&chunk_wav, 16000)?;
        writer.append_f32_samples(slice)?;
        writer.pad_to_minimum_samples(16000)?;
        writer.finish()?;

        let mut cmd = Command::new(&exe);
        cmd.arg(format!(
            "--tokens={}",
            model_dir.join("tokens.txt").display()
        ))
        .arg(format!(
            "--encoder={}",
            model_dir.join("encoder.int8.onnx").display()
        ))
        .arg(format!(
            "--decoder={}",
            model_dir.join("decoder.int8.onnx").display()
        ))
        .arg(format!(
            "--joiner={}",
            model_dir.join("joiner.int8.onnx").display()
        ))
        .arg("--model-type=nemo_transducer")
        .arg(format!("--num-threads={}", optimal_threads()))
        .arg(&chunk_wav);
        prepend_runtime_to_path_env(&mut cmd);
        apply_no_window(&mut cmd);

        let out = cmd.output();
        let _ = fs::remove_file(&chunk_wav);

        let out = out.map_err(|e| {
            SidecarError::InferenceFailed(format!("Failed to run sherpa-onnx-offline: {}", e))
        })?;
        let combined = format!(
            "{}\n{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
        for line in combined.lines() {
            let trimmed = line.trim();
            if trimmed.starts_with('{') && trimmed.ends_with('}') && trimmed.contains("\"text\"") {
                let (w, t) = parse_sherpa_result_json(trimmed, offset_sec, dur_sec);
                all_words.extend(w);
                if !t.is_empty() {
                    if !full_text.is_empty() {
                        full_text.push(' ');
                    }
                    full_text.push_str(&t);
                }
            }
        }

        offset = end;
        let pct = (((offset as f64) / (total_samples as f64)) * 99.0).round() as i32;
        emit_progress("transcribing", pct.clamp(1, 99));
    }

    Ok((all_words, full_text))
}

fn run_parakeet_asr(
    model_dir: &Path,
    wav_path: &Path,
) -> Result<(Vec<WordTiming>, String), SidecarError> {
    if let Ok(res) = run_parakeet_via_c_api(model_dir, wav_path) {
        return Ok(res);
    }
    run_parakeet_via_cli(model_dir, wav_path)
}

// MARK: - Speaker Diarization (Sherpa-ONNX C-API DLL + CLI fallback)

#[repr(C)]
struct SherpaOnnxOfflineSpeakerSegmentationPyannoteModelConfig {
    model: *const c_char,
}

#[repr(C)]
struct SherpaOnnxOfflineSpeakerSegmentationModelConfig {
    pyannote: SherpaOnnxOfflineSpeakerSegmentationPyannoteModelConfig,
    num_threads: c_int,
    debug: c_int,
    provider: *const c_char,
}

#[repr(C)]
struct SherpaOnnxSpeakerEmbeddingExtractorConfig {
    model: *const c_char,
    num_threads: c_int,
    debug: c_int,
    provider: *const c_char,
}

#[repr(C)]
struct SherpaOnnxFastClusteringConfig {
    num_clusters: c_int,
    threshold: c_float,
}

#[repr(C)]
struct SherpaOnnxOfflineSpeakerDiarizationConfig {
    segmentation: SherpaOnnxOfflineSpeakerSegmentationModelConfig,
    embedding: SherpaOnnxSpeakerEmbeddingExtractorConfig,
    clustering: SherpaOnnxFastClusteringConfig,
    min_duration_on: c_float,
    min_duration_off: c_float,
}

#[repr(C)]
struct SherpaOnnxOfflineSpeakerDiarizationSegment {
    start: c_float,
    end: c_float,
    speaker: c_int,
}

static LAST_DIAR_PCT: AtomicI32 = AtomicI32::new(0);

unsafe extern "C" fn diar_progress_callback(
    num_processed_chunks: c_int,
    num_total_chunks: c_int,
    _arg: *mut c_void,
) -> c_int {
    if num_total_chunks > 0 {
        let pct = (((num_processed_chunks as f64) / (num_total_chunks as f64)) * 95.0).round() as i32;
        let clamped = pct.clamp(1, 95);
        let prev = LAST_DIAR_PCT.load(Ordering::Relaxed);
        if clamped >= prev + 5 {
            LAST_DIAR_PCT.store(clamped, Ordering::Relaxed);
            emit_progress("diarizing", clamped);
        }
    }
    0
}

pub fn parse_speaker_clustering_hint(speakers_hint: &str) -> (i32, f32) {
    let norm = speakers_hint.trim().to_ascii_lowercase();
    if norm.is_empty() || norm == "auto" {
        (-1, 0.50)
    } else if norm == "4+" {
        // Lower agglomerative threshold encourages separating 4+ distinct voices
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

fn run_diarization_via_c_api(
    seg_model: &Path,
    emb_model: &Path,
    wav_path: &Path,
    speakers_hint: &str,
) -> Result<Vec<TimedSpeakerSegment>, String> {
    let dll_path = find_runtime_library("sherpa-onnx-c-api")
        .ok_or_else(|| "sherpa-onnx-c-api shared library not found".to_string())?;
    let lib = DynLib::open(&dll_path)
        .ok_or_else(|| format!("Failed to load {}", dll_path.display()))?;

    type CreateDiarFn =
        unsafe extern "C" fn(*const SherpaOnnxOfflineSpeakerDiarizationConfig) -> *const c_void;
    type DestroyDiarFn = unsafe extern "C" fn(*const c_void);
    type ProcessWithCbFn = unsafe extern "C" fn(
        *const c_void,
        *const c_float,
        c_int,
        unsafe extern "C" fn(c_int, c_int, *mut c_void) -> c_int,
        *mut c_void,
    ) -> *const c_void;
    type GetNumSegmentsFn = unsafe extern "C" fn(*const c_void) -> c_int;
    type SortByStartTimeFn =
        unsafe extern "C" fn(*const c_void) -> *const SherpaOnnxOfflineSpeakerDiarizationSegment;
    type DestroySegmentFn =
        unsafe extern "C" fn(*const SherpaOnnxOfflineSpeakerDiarizationSegment);
    type DestroyResultFn = unsafe extern "C" fn(*const c_void);

    unsafe {
        let create_sd: CreateDiarFn = lib
            .sym(b"SherpaOnnxCreateOfflineSpeakerDiarization\0")
            .ok_or("Missing SherpaOnnxCreateOfflineSpeakerDiarization")?;
        let destroy_sd: DestroyDiarFn = lib
            .sym(b"SherpaOnnxDestroyOfflineSpeakerDiarization\0")
            .ok_or("Missing SherpaOnnxDestroyOfflineSpeakerDiarization")?;
        let process_cb: ProcessWithCbFn = lib
            .sym(b"SherpaOnnxOfflineSpeakerDiarizationProcessWithCallback\0")
            .ok_or("Missing SherpaOnnxOfflineSpeakerDiarizationProcessWithCallback")?;
        let get_num: GetNumSegmentsFn = lib
            .sym(b"SherpaOnnxOfflineSpeakerDiarizationResultGetNumSegments\0")
            .ok_or("Missing SherpaOnnxOfflineSpeakerDiarizationResultGetNumSegments")?;
        let sort_segs: SortByStartTimeFn = lib
            .sym(b"SherpaOnnxOfflineSpeakerDiarizationResultSortByStartTime\0")
            .ok_or("Missing SherpaOnnxOfflineSpeakerDiarizationResultSortByStartTime")?;
        let destroy_seg: DestroySegmentFn = lib
            .sym(b"SherpaOnnxOfflineSpeakerDiarizationDestroySegment\0")
            .ok_or("Missing SherpaOnnxOfflineSpeakerDiarizationDestroySegment")?;
        let destroy_res: DestroyResultFn = lib
            .sym(b"SherpaOnnxOfflineSpeakerDiarizationDestroyResult\0")
            .ok_or("Missing SherpaOnnxOfflineSpeakerDiarizationDestroyResult")?;

        let seg_c = CString::new(seg_model.to_string_lossy().as_bytes()).map_err(|e| e.to_string())?;
        let emb_c = CString::new(emb_model.to_string_lossy().as_bytes()).map_err(|e| e.to_string())?;
        let provider_c = CString::new("cpu").unwrap();
        let (num_clusters, threshold) = parse_speaker_clustering_hint(speakers_hint);
        let threads = optimal_threads() as c_int;

        let cfg = SherpaOnnxOfflineSpeakerDiarizationConfig {
            segmentation: SherpaOnnxOfflineSpeakerSegmentationModelConfig {
                pyannote: SherpaOnnxOfflineSpeakerSegmentationPyannoteModelConfig {
                    model: seg_c.as_ptr(),
                },
                num_threads: threads,
                debug: 0,
                provider: provider_c.as_ptr(),
            },
            embedding: SherpaOnnxSpeakerEmbeddingExtractorConfig {
                model: emb_c.as_ptr(),
                num_threads: threads,
                debug: 0,
                provider: provider_c.as_ptr(),
            },
            clustering: SherpaOnnxFastClusteringConfig {
                num_clusters,
                threshold,
            },
            min_duration_on: 0.3,
            min_duration_off: 0.5,
        };

        let sd = create_sd(&cfg);
        if sd.is_null() {
            return Err("SherpaOnnxCreateOfflineSpeakerDiarization returned null".to_string());
        }

        LAST_DIAR_PCT.store(0, Ordering::Relaxed);
        let samples = load_normalized_wav_f32(wav_path).map_err(|e| e.message().to_string())?;
        let result = process_cb(
            sd,
            samples.as_ptr(),
            samples.len() as c_int,
            diar_progress_callback,
            std::ptr::null_mut(),
        );

        let mut out_segments = Vec::new();
        if !result.is_null() {
            let n = get_num(result);
            if n > 0 {
                let seg_ptr = sort_segs(result);
                if !seg_ptr.is_null() {
                    let slice = std::slice::from_raw_parts(seg_ptr, n as usize);
                    for s in slice {
                        out_segments.push(TimedSpeakerSegment {
                            speaker_id: format!("S{}", s.speaker),
                            start: (s.start as f64).max(0.0),
                            end: (s.end as f64).max(s.start as f64),
                        });
                    }
                    destroy_seg(seg_ptr);
                }
            }
            destroy_res(result);
        }

        destroy_sd(sd);
        Ok(out_segments)
    }
}

pub fn parse_sherpa_diarization_stdout(output: &str) -> Vec<TimedSpeakerSegment> {
    let mut segs = Vec::new();
    for line in output.lines() {
        // Format: "0.23 -- 4.56 speaker_00"
        if let Some(dash_pos) = line.find("--") {
            let left = line[..dash_pos].trim();
            let right = line[dash_pos + 2..].trim();
            let start_tok = left.split_whitespace().last().unwrap_or("");
            let mut right_parts = right.split_whitespace();
            let end_tok = right_parts.next().unwrap_or("");
            let spk_tok = right_parts.next().unwrap_or("speaker_00");
            if let (Ok(s), Ok(e)) = (start_tok.parse::<f64>(), end_tok.parse::<f64>()) {
                segs.push(TimedSpeakerSegment {
                    speaker_id: spk_tok.to_string(),
                    start: s.max(0.0),
                    end: e.max(s),
                });
            }
        }
    }
    segs
}

fn run_diarization_via_cli(
    seg_model: &Path,
    emb_model: &Path,
    wav_path: &Path,
    speakers_hint: &str,
) -> Result<Vec<TimedSpeakerSegment>, SidecarError> {
    let exe = find_runtime_or_system_exe("sherpa-onnx-offline-speaker-diarization").ok_or_else(|| {
        SidecarError::InferenceFailed(
            "Speaker diarization runtime ('sherpa-onnx-c-api.dll' / 'sherpa-onnx-offline-speaker-diarization.exe') was not found in _internal/audio_runtime.".to_string(),
        )
    })?;

    let (num_clusters, threshold) = parse_speaker_clustering_hint(speakers_hint);
    let mut cmd = Command::new(exe);
    cmd.arg(format!(
        "--segmentation.pyannote-model={}",
        seg_model.display()
    ))
    .arg(format!("--embedding.model={}", emb_model.display()))
    .arg(format!("--clustering.num-clusters={}", num_clusters))
    .arg(format!("--clustering.threshold={:.2}", threshold))
    .arg(&wav_path);
    prepend_runtime_to_path_env(&mut cmd);
    apply_no_window(&mut cmd);

    let out = cmd.output().map_err(|e| {
        SidecarError::InferenceFailed(format!("Failed to launch diarization helper: {}", e))
    })?;

    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
        return Err(SidecarError::InferenceFailed(format!(
            "Speaker diarization failed: {}",
            err
        )));
    }

    let combined = format!(
        "{}\n{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    Ok(parse_sherpa_diarization_stdout(&combined))
}

fn run_sherpa_diarization(
    models_root: &Path,
    wav_path: &Path,
    speakers_hint: &str,
) -> Result<Vec<TimedSpeakerSegment>, SidecarError> {
    let seg_model = models_root.join("diarization").join("segmentation.int8.onnx");
    let emb_model = models_root.join("diarization").join("embedding.onnx");
    if !seg_model.is_file() || !emb_model.is_file() {
        return Err(SidecarError::ModelMissing(format!(
            "Speaker diarization weights are missing in {}. Re-download your speech model or pass --no-diarize.",
            models_root.join("diarization").display()
        )));
    }

    if let Ok(segs) = run_diarization_via_c_api(&seg_model, &emb_model, wav_path, speakers_hint) {
        return Ok(segs);
    }
    run_diarization_via_cli(&seg_model, &emb_model, wav_path, speakers_hint)
}

// MARK: - Main Transcribe Orchestrator

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
    let (_resolved_id, engine, model_path) =
        resolve_model_and_engine(models_root, model_id_arg)?;

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

    emit_progress("transcribing", 0);
    let (words, fallback_text) = match engine {
        WindowsEngine::Whisper => run_whisper_asr(&model_path, &norm_wav.wav_path)?,
        WindowsEngine::SherpaParakeet => run_parakeet_asr(&model_path, &norm_wav.wav_path)?,
    };
    emit_progress("transcribing", 100);

    if !diarize {
        let segments =
            build_undiarized_segments(&words, &fallback_text, norm_wav.duration_sec);
        emit_result(round_millis(norm_wav.duration_sec), 0, &segments);
        return Ok(());
    }

    emit_progress("diarizing", 0);
    let speaker_segments =
        run_sherpa_diarization(models_root, &norm_wav.wav_path, speakers_hint)?;
    emit_progress("diarizing", 100);

    let (segments, speaker_count) = build_diarized_segments(
        &words,
        &fallback_text,
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
    fn test_parse_whisper_json_output_extracts_words() {
        let sample = r#"{
            "transcription": [
                {
                    "offsets": { "from": 0, "to": 1800 },
                    "text": " Good morning team.",
                    "tokens": [
                        { "text": "[_BEG_]", "offsets": { "from": 0, "to": 0 } },
                        { "text": " Good", "offsets": { "from": 100, "to": 450 } },
                        { "text": " morn", "offsets": { "from": 460, "to": 750 } },
                        { "text": "ing", "offsets": { "from": 750, "to": 980 } },
                        { "text": " team", "offsets": { "from": 1020, "to": 1600 } },
                        { "text": ".", "offsets": { "from": 1600, "to": 1700 } }
                    ]
                }
            ]
        }"#;
        let (words, text) = parse_whisper_json_output(sample).unwrap();
        assert_eq!(text, "Good morning team.");
        assert_eq!(words.len(), 3);
        assert_eq!(words[0].word, "Good");
        assert_eq!(words[1].word, "morning");
        assert_eq!(words[2].word, "team.");
    }

    #[test]
    fn test_parse_sherpa_diarization_stdout() {
        let out = "0.15 -- 3.40 speaker_01\n3.60 -- 7.20 speaker_00\n";
        let segs = parse_sherpa_diarization_stdout(out);
        assert_eq!(segs.len(), 2);
        assert_eq!(segs[0].speaker_id, "speaker_01");
        assert!((segs[0].start - 0.15).abs() < 1e-6);
        assert_eq!(segs[1].speaker_id, "speaker_00");
    }
}
