pub mod align;
pub mod decode;
pub mod hardware;
pub mod json;
pub mod probe;
pub mod transcribe;

use std::env;
use std::process;

use crate::hardware::detect_hardware;
use crate::json::{emit_download, emit_error, emit_hardware, emit_probe_entries};
use crate::probe::{probe_single_file, ProbeEntry};
use crate::transcribe::{resolve_model_and_engine, run_transcribe};

#[derive(Debug, Clone)]
pub enum SidecarError {
    InvalidArgs(String),
    FileNotFound(String),
    ModelMissing(String),
    NoAudioTrack(String),
    DecodeFailed(String),
    InferenceFailed(String),
}

impl SidecarError {
    pub fn code(&self) -> &'static str {
        match self {
            SidecarError::InvalidArgs(_) => "invalid_args",
            SidecarError::FileNotFound(_) => "file_not_found",
            SidecarError::ModelMissing(_) => "model_missing",
            SidecarError::NoAudioTrack(_) => "no_audio",
            SidecarError::DecodeFailed(_) => "decode_error",
            SidecarError::InferenceFailed(_) => "inference_error",
        }
    }

    pub fn exit_code(&self) -> i32 {
        match self {
            SidecarError::InvalidArgs(_)
            | SidecarError::FileNotFound(_)
            | SidecarError::ModelMissing(_) => 2,
            SidecarError::NoAudioTrack(_) => 3,
            SidecarError::DecodeFailed(_) | SidecarError::InferenceFailed(_) => 1,
        }
    }

    pub fn message(&self) -> &str {
        match self {
            SidecarError::InvalidArgs(m)
            | SidecarError::FileNotFound(m)
            | SidecarError::ModelMissing(m)
            | SidecarError::NoAudioTrack(m)
            | SidecarError::DecodeFailed(m)
            | SidecarError::InferenceFailed(m) => m.as_str(),
        }
    }
}

fn dispatch_cli(args: &[String]) -> Result<(), SidecarError> {
    let first = match args.first() {
        Some(f) => f.as_str(),
        None => {
            return Err(SidecarError::InvalidArgs(
                "Usage: docprep-audio [--hardware | --probe <files...> | --download --models-root <dir> | --transcribe <file> --models-root <dir>]".to_string(),
            ));
        }
    };

    match first {
        "--hardware" => {
            let hw = detect_hardware();
            emit_hardware(&hw);
            Ok(())
        }

        "--probe" => {
            let paths = &args[1..];
            let mut entries: Vec<ProbeEntry> = Vec::with_capacity(paths.len());
            for p in paths {
                entries.push(probe_single_file(p));
            }
            emit_probe_entries(&entries);
            Ok(())
        }

        "--download" => {
            let mut models_root: Option<String> = None;
            let mut model_id = String::new();
            let mut i = 1usize;
            while i < args.len() {
                match args[i].as_str() {
                    "--models-root" => {
                        i += 1;
                        if i < args.len() {
                            models_root = Some(args[i].clone());
                        }
                    }
                    "--model-id" => {
                        i += 1;
                        if i < args.len() {
                            model_id = args[i].clone();
                        }
                    }
                    "--with-diarizer" | "--no-diarizer" => {}
                    _ => {}
                }
                i += 1;
            }
            let root = models_root.filter(|s| !s.is_empty()).ok_or_else(|| {
                SidecarError::InvalidArgs("--download requires --models-root <dir>".to_string())
            })?;

            // On Windows, `docprep_core.py --download-model` / `backend.models.download_model`
            // downloads ONNX & GGML weights directly via HTTP with Range resume and SHA-256 verification.
            // If `--download` is invoked directly on the sidecar and the model is already on disk, emit complete.
            if resolve_model_and_engine(std::path::Path::new(&root), &model_id).is_ok() {
                emit_download("complete", 100, 1, 1);
                Ok(())
            } else {
                Err(SidecarError::InvalidArgs(
                    "Windows models are downloaded by docprep-core (--download-model <id>).".to_string(),
                ))
            }
        }

        "--transcribe" => {
            if args.len() < 2 {
                return Err(SidecarError::InvalidArgs(
                    "--transcribe requires <file>".to_string(),
                ));
            }
            let file_path = &args[1];
            let mut models_root: Option<String> = None;
            let mut model_id = String::new();
            let mut diarize = false;
            let mut speakers = String::from("auto");

            let mut i = 2usize;
            while i < args.len() {
                match args[i].as_str() {
                    "--models-root" => {
                        i += 1;
                        if i < args.len() {
                            models_root = Some(args[i].clone());
                        }
                    }
                    "--model-id" => {
                        i += 1;
                        if i < args.len() {
                            model_id = args[i].clone();
                        }
                    }
                    "--diarize" => {
                        diarize = true;
                    }
                    "--no-diarize" => {
                        diarize = false;
                    }
                    "--speakers" => {
                        i += 1;
                        if i < args.len() {
                            speakers = args[i].clone();
                        }
                    }
                    _ => {}
                }
                i += 1;
            }

            let root = models_root.filter(|s| !s.is_empty()).ok_or_else(|| {
                SidecarError::InvalidArgs(
                    "--transcribe requires --models-root <dir>".to_string(),
                )
            })?;

            run_transcribe(file_path, &root, &model_id, diarize, &speakers)
        }

        other => Err(SidecarError::InvalidArgs(format!("Unknown flag: {}", other))),
    }
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if let Err(err) = dispatch_cli(&args) {
        emit_error(err.code(), err.message());
        process::exit(err.exit_code());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::decode::WavStreamWriter;
    use std::fs::{self, File};
    use std::io::Write;

    #[test]
    fn test_cli_dispatch_errors_and_end_to_end_transcribe() {
        let base = env::temp_dir().join(format!("docprep-win-e2e-{}", process::id()));
        let _ = fs::remove_dir_all(&base);
        let models_root = base.join("models");
        let runtime_dir = base.join("runtime");
        fs::create_dir_all(&models_root).unwrap();
        fs::create_dir_all(&runtime_dir).unwrap();

        // 1. Missing file -> FileNotFound (exit code 2)
        let err = dispatch_cli(&[
            "--transcribe".to_string(),
            base.join("missing.wav").to_string_lossy().into_owned(),
            "--models-root".to_string(),
            models_root.to_string_lossy().into_owned(),
        ])
        .unwrap_err();
        assert_eq!(err.code(), "file_not_found");
        assert_eq!(err.exit_code(), 2);

        // 2. Create a valid 1.5s WAV file
        let wav_path = base.join("sample.wav");
        let mut writer = WavStreamWriter::create(&wav_path, 16000).unwrap();
        let samples: Vec<f32> = (0..24000)
            .map(|i| ((i as f32) * 0.05).sin() * 0.25)
            .collect();
        writer.append_f32_samples(&samples).unwrap();
        writer.finish().unwrap();

        // 3. Missing model -> ModelMissing (exit code 2)
        let err = dispatch_cli(&[
            "--transcribe".to_string(),
            wav_path.to_string_lossy().into_owned(),
            "--models-root".to_string(),
            models_root.to_string_lossy().into_owned(),
            "--model-id".to_string(),
            "whisper-small-q5_1".to_string(),
        ])
        .unwrap_err();
        assert_eq!(err.code(), "model_missing");
        assert_eq!(err.exit_code(), 2);

        // 4. Create dummy Whisper model + diarization weights + mock runtime binaries
        let whisper_dir = models_root.join("whisper-small-q5_1");
        let diar_dir = models_root.join("diarization");
        fs::create_dir_all(&whisper_dir).unwrap();
        fs::create_dir_all(&diar_dir).unwrap();
        fs::write(whisper_dir.join("ggml-model.bin"), b"dummy-ggml").unwrap();
        fs::write(diar_dir.join("segmentation.int8.onnx"), b"dummy-seg").unwrap();
        fs::write(diar_dir.join("embedding.onnx"), b"dummy-emb").unwrap();

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let whisper_mock = runtime_dir.join("whisper-cli");
            let mut f = File::create(&whisper_mock).unwrap();
            writeln!(
                f,
                r#"#!/bin/sh
OUT_PREFIX=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-of" ]; then
    shift
    OUT_PREFIX="$1"
  fi
  shift
done
echo "whisper_print_progress_callback: progress =  50%" >&2
cat << 'EOF' > "${{OUT_PREFIX}}.json"
{{"transcription":[{{"offsets":{{"from":0,"to":1500}},"text":" Hello world.","tokens":[{{"text":" Hello","offsets":{{"from":0,"to":600}}}},{{"text":" world.","offsets":{{"from":650,"to":1400}}}}]}}]}}
EOF
"#
            )
            .unwrap();
            let mut perms = fs::metadata(&whisper_mock).unwrap().permissions();
            perms.set_mode(0o755);
            fs::set_permissions(&whisper_mock, perms).unwrap();

            let diar_mock = runtime_dir.join("sherpa-onnx-offline-speaker-diarization");
            let mut df = File::create(&diar_mock).unwrap();
            writeln!(
                df,
                "#!/bin/sh\necho \"0.00 -- 0.62 speaker_01\"\necho \"0.63 -- 1.50 speaker_00\""
            )
            .unwrap();
            let mut dperms = fs::metadata(&diar_mock).unwrap().permissions();
            dperms.set_mode(0o755);
            fs::set_permissions(&diar_mock, dperms).unwrap();

            env::set_var("DOCPREP_AUDIO_RUNTIME_DIR", &runtime_dir);
            let res = dispatch_cli(&[
                "--transcribe".to_string(),
                wav_path.to_string_lossy().into_owned(),
                "--models-root".to_string(),
                models_root.to_string_lossy().into_owned(),
                "--model-id".to_string(),
                "whisper-small-q5_1".to_string(),
                "--diarize".to_string(),
                "--speakers".to_string(),
                "2".to_string(),
            ]);
            env::remove_var("DOCPREP_AUDIO_RUNTIME_DIR");
            assert!(res.is_ok(), "full diarized transcription must succeed");
        }

        let _ = fs::remove_dir_all(&base);
    }
}
