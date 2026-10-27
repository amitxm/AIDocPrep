use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::probe::{probe_matroska, probe_mp4, probe_with_ffprobe};
use crate::transcribe::{apply_no_window, find_runtime_or_system_exe};
use crate::SidecarError;

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);

pub struct NormalizedWav {
    pub wav_path: PathBuf,
    pub duration_sec: f64,
    pub sample_count: usize,
}

impl Drop for NormalizedWav {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.wav_path);
    }
}

pub struct WavStreamWriter {
    file: File,
    pub data_bytes_written: u32,
    pub sample_rate: u32,
}

impl WavStreamWriter {
    pub fn create(path: &Path, sample_rate: u32) -> Result<Self, SidecarError> {
        let file = File::create(path).map_err(|e| {
            SidecarError::DecodeFailed(format!(
                "Failed to create temporary WAV {}: {}",
                path.display(),
                e
            ))
        })?;
        let mut writer = Self {
            file,
            data_bytes_written: 0,
            sample_rate,
        };
        writer.write_header(0)?;
        Ok(writer)
    }

    fn write_header(&mut self, pcm_byte_count: u32) -> Result<(), SidecarError> {
        self.file
            .seek(SeekFrom::Start(0))
            .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
        let mut hdr = [0u8; 44];
        hdr[0..4].copy_from_slice(b"RIFF");
        let riff_size = 36u32.saturating_add(pcm_byte_count);
        hdr[4..8].copy_from_slice(&riff_size.to_le_bytes());
        hdr[8..16].copy_from_slice(b"WAVEfmt ");
        hdr[16..20].copy_from_slice(&16u32.to_le_bytes()); // fmt chunk size
        hdr[20..22].copy_from_slice(&1u16.to_le_bytes()); // PCM = 1
        hdr[22..24].copy_from_slice(&1u16.to_le_bytes()); // 1 channel (mono)
        hdr[24..28].copy_from_slice(&self.sample_rate.to_le_bytes());
        let byte_rate = self.sample_rate.saturating_mul(2);
        hdr[28..32].copy_from_slice(&byte_rate.to_le_bytes());
        hdr[32..34].copy_from_slice(&2u16.to_le_bytes()); // block align = 2
        hdr[34..36].copy_from_slice(&16u16.to_le_bytes()); // bits per sample = 16
        hdr[36..40].copy_from_slice(b"data");
        hdr[40..44].copy_from_slice(&pcm_byte_count.to_le_bytes());
        self.file
            .write_all(&hdr)
            .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
        Ok(())
    }

    pub fn append_pcm16_le_bytes(&mut self, bytes: &[u8]) -> Result<(), SidecarError> {
        if bytes.is_empty() {
            return Ok(());
        }
        self.file
            .write_all(bytes)
            .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
        self.data_bytes_written = self.data_bytes_written.saturating_add(bytes.len() as u32);
        Ok(())
    }

    pub fn append_f32_samples(&mut self, samples: &[f32]) -> Result<(), SidecarError> {
        if samples.is_empty() {
            return Ok(());
        }
        let mut buf = Vec::with_capacity(samples.len() * 2);
        for &s in samples {
            let clamped = if s.is_finite() { s.clamp(-1.0, 1.0) } else { 0.0 };
            let pcm = (clamped * 32767.0).round() as i16;
            buf.extend_from_slice(&pcm.to_le_bytes());
        }
        self.append_pcm16_le_bytes(&buf)
    }

    pub fn sample_count(&self) -> usize {
        (self.data_bytes_written as usize) / 2
    }

    pub fn pad_to_minimum_samples(&mut self, min_samples: usize) -> Result<(), SidecarError> {
        let cur = self.sample_count();
        if cur > 0 && cur < min_samples {
            let needed_bytes = (min_samples - cur) * 2;
            let zeros = vec![0u8; needed_bytes];
            self.append_pcm16_le_bytes(&zeros)?;
        }
        Ok(())
    }

    pub fn finish(mut self) -> Result<(), SidecarError> {
        let total = self.data_bytes_written;
        self.write_header(total)?;
        self.file
            .flush()
            .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
        Ok(())
    }
}

/// Read a normalized 16 kHz mono 16-bit PCM WAV file into `Vec<f32>` in `[-1.0, 1.0]`.
pub fn load_normalized_wav_f32(wav_path: &Path) -> Result<Vec<f32>, SidecarError> {
    let mut file = File::open(wav_path).map_err(|e| {
        SidecarError::DecodeFailed(format!(
            "Cannot open normalized WAV {}: {}",
            wav_path.display(),
            e
        ))
    })?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
    if bytes.len() < 44 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return Err(SidecarError::DecodeFailed(
            "Invalid normalized WAV header".to_string(),
        ));
    }

    let mut pos = 12usize;
    while pos + 8 <= bytes.len() {
        let id = &bytes[pos..pos + 4];
        let sz = u32::from_le_bytes(bytes[pos + 4..pos + 8].try_into().unwrap()) as usize;
        let data_start = pos + 8;
        if id == b"data" {
            let data_end = data_start.saturating_add(sz).min(bytes.len());
            let pcm = &bytes[data_start..data_end];
            let mut out = Vec::with_capacity(pcm.len() / 2);
            for chunk in pcm.chunks_exact(2) {
                let sample = i16::from_le_bytes([chunk[0], chunk[1]]);
                out.push((sample as f32) / 32768.0);
            }
            return Ok(out);
        }
        pos = data_start.saturating_add(sz + (sz & 1));
    }

    Ok(Vec::new())
}

// MARK: - Resampler (Arbitrary Sample Rate -> 16 kHz Mono)

pub fn resample_mono_f32(input: &[f32], src_rate: u32, dst_rate: u32) -> Vec<f32> {
    if input.is_empty() || src_rate == 0 || dst_rate == 0 {
        return Vec::new();
    }
    if src_rate == dst_rate {
        return input.to_vec();
    }

    // Fast exact 3:1 decimation with 3-tap low-pass filter for 48 kHz -> 16 kHz
    if src_rate == 48000 && dst_rate == 16000 && input.len() >= 3 {
        let out_len = input.len() / 3;
        let mut out = Vec::with_capacity(out_len);
        for i in 0..out_len {
            let idx = i * 3;
            let s0 = input[idx];
            let s1 = input.get(idx + 1).copied().unwrap_or(s0);
            let s2 = input.get(idx + 2).copied().unwrap_or(s1);
            out.push(0.25 * s0 + 0.50 * s1 + 0.25 * s2);
        }
        return out;
    }

    let ratio = (src_rate as f64) / (dst_rate as f64);
    let out_len = ((input.len() as f64) / ratio).round() as usize;
    if out_len == 0 {
        return Vec::new();
    }

    let mut out = Vec::with_capacity(out_len);
    let max_idx = input.len() - 1;

    if ratio > 1.0 {
        let half_win = (0.5 * ratio).max(0.5);
        for i in 0..out_len {
            let center = (i as f64 + 0.5) * ratio - 0.5;
            let left = (center - half_win).floor().max(0.0) as usize;
            let right = (center + half_win).ceil().min(max_idx as f64) as usize;
            if left >= right {
                let idx = (center.round() as usize).min(max_idx);
                out.push(input[idx]);
            } else {
                let mut sum = 0.0f32;
                let mut weight_sum = 0.0f32;
                for j in left..=right {
                    let dist = ((j as f64 - center).abs() / half_win) as f32;
                    let w = (1.0 - dist).max(0.05);
                    sum += input[j] * w;
                    weight_sum += w;
                }
                out.push(if weight_sum > 0.0 { sum / weight_sum } else { 0.0 });
            }
        }
    } else {
        for i in 0..out_len {
            let src_pos = (i as f64) * ratio;
            let idx0 = (src_pos.floor() as usize).min(max_idx);
            let idx1 = (idx0 + 1).min(max_idx);
            let frac = (src_pos - (idx0 as f64)) as f32;
            out.push(input[idx0] * (1.0 - frac) + input[idx1] * frac);
        }
    }

    out
}

// MARK: - 1. Native Multi-Format WAV Decoder (PCM 8/16/24/32 + IEEE Float 32/64 + Extensible)

pub fn decode_native_wav(
    input_path: &Path,
    output_wav_path: &Path,
) -> Result<(f64, usize), SidecarError> {
    let mut file = File::open(input_path).map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
    let mut hdr = [0u8; 12];
    file.read_exact(&mut hdr)
        .map_err(|_| SidecarError::DecodeFailed("Not a valid RIFF/WAV file".to_string()))?;
    if &hdr[0..4] != b"RIFF" || &hdr[8..12] != b"WAVE" {
        return Err(SidecarError::DecodeFailed(
            "Not a RIFF WAVE container".to_string(),
        ));
    }

    let file_len = file
        .seek(SeekFrom::End(0))
        .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
    file.seek(SeekFrom::Start(12))
        .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;

    let mut format_tag: u16 = 0;
    let mut channels: u16 = 0;
    let mut sample_rate: u32 = 0;
    let mut bits_per_sample: u16 = 0;
    let mut data_offset: u64 = 0;
    let mut data_size: u64 = 0;

    while let Ok(pos) = file.stream_position() {
        if pos + 8 > file_len {
            break;
        }
        let mut ch = [0u8; 8];
        if file.read_exact(&mut ch).is_err() {
            break;
        }
        let cid = &ch[0..4];
        let csz = u32::from_le_bytes([ch[4], ch[5], ch[6], ch[7]]) as u64;

        if cid == b"fmt " && csz >= 16 {
            let mut fmt = vec![0u8; (csz as usize).min(64)];
            file.read_exact(&mut fmt)
                .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
            format_tag = u16::from_le_bytes([fmt[0], fmt[1]]);
            channels = u16::from_le_bytes([fmt[2], fmt[3]]);
            sample_rate = u32::from_le_bytes([fmt[4], fmt[5], fmt[6], fmt[7]]);
            bits_per_sample = u16::from_le_bytes([fmt[14], fmt[15]]);
            // WAVE_FORMAT_EXTENSIBLE (0xFFFE): subformat GUID first 2 bytes at offset 24
            if format_tag == 0xFFFE && fmt.len() >= 26 {
                format_tag = u16::from_le_bytes([fmt[24], fmt[25]]);
            }
        } else if cid == b"data" {
            data_offset = pos + 8;
            data_size = if csz == 0xFFFF_FFFF {
                file_len.saturating_sub(data_offset)
            } else {
                csz.min(file_len.saturating_sub(data_offset))
            };
            break;
        }

        let padded = csz + (csz & 1);
        let next = pos.saturating_add(8).saturating_add(padded);
        if next >= file_len || file.seek(SeekFrom::Start(next)).is_err() {
            break;
        }
    }

    if (format_tag != 1 && format_tag != 3)
        || channels == 0
        || sample_rate == 0
        || bits_per_sample == 0
    {
        return Err(SidecarError::DecodeFailed(format!(
            "Unsupported WAV encoding (tag={}, ch={}, sr={}, bps={})",
            format_tag, channels, sample_rate, bits_per_sample
        )));
    }

    let bytes_per_sample = (bits_per_sample / 8) as usize;
    let frame_bytes = bytes_per_sample * (channels as usize);
    if frame_bytes == 0 || data_offset == 0 {
        return Err(SidecarError::DecodeFailed(
            "WAV missing valid audio data chunk".to_string(),
        ));
    }

    file.seek(SeekFrom::Start(data_offset))
        .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
    let mut raw = vec![0u8; data_size as usize];
    let n_read = file
        .read(&mut raw)
        .map_err(|e| SidecarError::DecodeFailed(e.to_string()))?;
    raw.truncate(n_read);

    let total_frames = raw.len() / frame_bytes;
    let mut mono = Vec::with_capacity(total_frames);
    let ch_count = channels as usize;
    let inv_ch = 1.0f32 / (ch_count as f32);

    for frame in raw.chunks_exact(frame_bytes) {
        let mut sum = 0.0f32;
        for c in 0..ch_count {
            let s_bytes = &frame[c * bytes_per_sample..(c + 1) * bytes_per_sample];
            let val = match (format_tag, bits_per_sample) {
                (1, 8) => ((s_bytes[0] as f32) - 128.0) / 128.0,
                (1, 16) => (i16::from_le_bytes([s_bytes[0], s_bytes[1]]) as f32) / 32768.0,
                (1, 24) => {
                    let sign_ext = if (s_bytes[2] & 0x80) != 0 { 0xFFu8 } else { 0x00u8 };
                    let v = i32::from_le_bytes([s_bytes[0], s_bytes[1], s_bytes[2], sign_ext]);
                    (v as f32) / 8_388_608.0
                }
                (1, 32) => {
                    let v = i32::from_le_bytes([s_bytes[0], s_bytes[1], s_bytes[2], s_bytes[3]]);
                    (v as f32) / 2_147_483_648.0
                }
                (3, 32) => f32::from_le_bytes([s_bytes[0], s_bytes[1], s_bytes[2], s_bytes[3]]),
                (3, 64) => f64::from_le_bytes(s_bytes[0..8].try_into().unwrap()) as f32,
                _ => 0.0,
            };
            sum += val;
        }
        mono.push(sum * inv_ch);
    }

    let real_duration = (total_frames as f64) / (sample_rate as f64);
    let resampled = resample_mono_f32(&mono, sample_rate, 16000);
    let mut writer = WavStreamWriter::create(output_wav_path, 16000)?;
    writer.append_f32_samples(&resampled)?;
    writer.pad_to_minimum_samples(16000)?;
    let final_samples = writer.sample_count();
    writer.finish()?;

    Ok((real_duration, final_samples))
}

// MARK: - 2. Windows Media Foundation Streaming Decoder & Prober

#[allow(dead_code, non_snake_case)]
pub mod win_mf {
    use super::*;
    use std::ffi::c_void;
    use std::ptr::null_mut;

    #[repr(C)]
    #[derive(Copy, Clone, PartialEq, Eq)]
    pub struct GUID {
        pub data1: u32,
        pub data2: u16,
        pub data3: u16,
        pub data4: [u8; 8],
    }

    const MF_VERSION: u32 = 0x0002_0070;
    const MFSTARTUP_NOSOCKET: u32 = 0x1;
    const COINIT_MULTITHREADED: u32 = 0x0;

    const MF_SOURCE_READER_FIRST_VIDEO_STREAM: u32 = 0xFFFF_FFFC;
    const MF_SOURCE_READER_FIRST_AUDIO_STREAM: u32 = 0xFFFF_FFFD;
    const MF_SOURCE_READER_ALL_STREAMS: u32 = 0xFFFF_FFFE;
    const MF_SOURCE_READER_MEDIASOURCE: u32 = 0xFFFF_FFFF;

    const MF_SOURCE_READERF_ERROR: u32 = 0x0000_0001;
    const MF_SOURCE_READERF_ENDOFSTREAM: u32 = 0x0000_0002;
    const MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED: u32 = 0x0000_0010;

    // MF_MT_MAJOR_TYPE = {48eba18e-f8c9-4687-bf11-0a74c9f96a8f}
    const MF_MT_MAJOR_TYPE: GUID = GUID {
        data1: 0x48eba18e,
        data2: 0xf8c9,
        data3: 0x4687,
        data4: [0xbf, 0x11, 0x0a, 0x74, 0xc9, 0xf9, 0x6a, 0x8f],
    };
    // MF_MT_SUBTYPE = {f7e34c9a-42e8-4714-b74b-cb29d72c35e5}
    const MF_MT_SUBTYPE: GUID = GUID {
        data1: 0xf7e34c9a,
        data2: 0x42e8,
        data3: 0x4714,
        data4: [0xb7, 0x4b, 0xcb, 0x29, 0xd7, 0x2c, 0x35, 0xe5],
    };
    // MF_MT_AUDIO_NUM_CHANNELS = {37e48bf5-645e-4c5b-89de-ada9e29b696a}
    const MF_MT_AUDIO_NUM_CHANNELS: GUID = GUID {
        data1: 0x37e48bf5,
        data2: 0x645e,
        data3: 0x4c5b,
        data4: [0x89, 0xde, 0xad, 0xa9, 0xe2, 0x9b, 0x69, 0x6a],
    };
    // MF_MT_AUDIO_SAMPLES_PER_SECOND = {5faeeae7-0290-4c31-9e8a-c534f68d9dba}
    const MF_MT_AUDIO_SAMPLES_PER_SECOND: GUID = GUID {
        data1: 0x5faeeae7,
        data2: 0x0290,
        data3: 0x4c31,
        data4: [0x9e, 0x8a, 0xc5, 0x34, 0xf6, 0x8d, 0x9d, 0xba],
    };
    // MF_MT_AUDIO_BITS_PER_SAMPLE = {f2deb57f-40fa-4764-aa33-ed4f2d1ff669}
    const MF_MT_AUDIO_BITS_PER_SAMPLE: GUID = GUID {
        data1: 0xf2deb57f,
        data2: 0x40fa,
        data3: 0x4764,
        data4: [0xaa, 0x33, 0xed, 0x4f, 0x2d, 0x1f, 0xf6, 0x69],
    };
    // MFMediaType_Audio = {73647561-0000-0010-8000-00AA00389B71}
    const MF_MEDIA_TYPE_AUDIO: GUID = GUID {
        data1: 0x73647561,
        data2: 0x0000,
        data3: 0x0010,
        data4: [0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71],
    };
    // MFAudioFormat_PCM = {00000001-0000-0010-8000-00AA00389B71}
    const MF_AUDIO_FORMAT_PCM: GUID = GUID {
        data1: 0x00000001,
        data2: 0x0000,
        data3: 0x0010,
        data4: [0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71],
    };
    // MFAudioFormat_Float = {00000003-0000-0010-8000-00AA00389B71}
    const MF_AUDIO_FORMAT_FLOAT: GUID = GUID {
        data1: 0x00000003,
        data2: 0x0000,
        data3: 0x0010,
        data4: [0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71],
    };
    // MF_PD_DURATION = {6c990d33-bb8e-477a-8598-0d5d96fcd88a}
    const MF_PD_DURATION: GUID = GUID {
        data1: 0x6c990d33,
        data2: 0xbb8e,
        data3: 0x477a,
        data4: [0x85, 0x98, 0x0d, 0x5d, 0x96, 0xfc, 0xd8, 0x8a],
    };

    #[repr(C)]
    pub struct PROPVARIANT {
        pub vt: u16,
        pub reserved1: u16,
        pub reserved2: u16,
        pub reserved3: u16,
        pub data: [u64; 2],
    }

    #[repr(C)]
    pub struct IMFMediaBufferVtbl {
        pub query_interface: *const c_void,
        pub add_ref: *const c_void,
        pub release: unsafe extern "system" fn(*mut c_void) -> u32,
        pub lock: unsafe extern "system" fn(*mut c_void, *mut *mut u8, *mut u32, *mut u32) -> i32,
        pub unlock: unsafe extern "system" fn(*mut c_void) -> i32,
        pub get_current_length: unsafe extern "system" fn(*mut c_void, *mut u32) -> i32,
        pub set_current_length: *const c_void,
        pub get_max_length: *const c_void,
    }

    #[repr(C)]
    pub struct IMFMediaBuffer {
        pub vtbl: *const IMFMediaBufferVtbl,
    }

    #[repr(C)]
    pub struct IMFSampleVtbl {
        pub query_interface: *const c_void,
        pub add_ref: *const c_void,
        pub release: unsafe extern "system" fn(*mut c_void) -> u32,
        pub attributes_methods: [*const c_void; 30],
        pub get_sample_flags: *const c_void,
        pub set_sample_flags: *const c_void,
        pub get_sample_time: *const c_void,
        pub set_sample_time: *const c_void,
        pub get_sample_duration: *const c_void,
        pub set_sample_duration: *const c_void,
        pub get_buffer_count: *const c_void,
        pub get_buffer_by_index: *const c_void,
        pub convert_to_contiguous_buffer:
            unsafe extern "system" fn(*mut c_void, *mut *mut IMFMediaBuffer) -> i32,
    }

    #[repr(C)]
    pub struct IMFSample {
        pub vtbl: *const IMFSampleVtbl,
    }

    #[repr(C)]
    pub struct IMFMediaTypeVtbl {
        pub query_interface: *const c_void,
        pub add_ref: *const c_void,
        pub release: unsafe extern "system" fn(*mut c_void) -> u32,
        pub get_item: *const c_void,
        pub get_item_type: *const c_void,
        pub compare_item: *const c_void,
        pub compare: *const c_void,
        pub get_uint32: unsafe extern "system" fn(*mut c_void, *const GUID, *mut u32) -> i32,
        pub get_uint64: *const c_void,
        pub get_double: *const c_void,
        pub get_guid: unsafe extern "system" fn(*mut c_void, *const GUID, *mut GUID) -> i32,
        pub get_string_length: *const c_void,
        pub get_string: *const c_void,
        pub get_allocated_string: *const c_void,
        pub get_blob_size: *const c_void,
        pub get_blob: *const c_void,
        pub get_allocated_blob: *const c_void,
        pub get_unknown: *const c_void,
        pub set_item: *const c_void,
        pub delete_item: *const c_void,
        pub delete_all_items: *const c_void,
        pub set_uint32: unsafe extern "system" fn(*mut c_void, *const GUID, u32) -> i32,
        pub set_uint64: *const c_void,
        pub set_double: *const c_void,
        pub set_guid: unsafe extern "system" fn(*mut c_void, *const GUID, *const GUID) -> i32,
    }

    #[repr(C)]
    pub struct IMFMediaType {
        pub vtbl: *const IMFMediaTypeVtbl,
    }

    #[repr(C)]
    pub struct IMFSourceReaderVtbl {
        pub query_interface: *const c_void,
        pub add_ref: *const c_void,
        pub release: unsafe extern "system" fn(*mut c_void) -> u32,
        pub get_stream_selection: *const c_void,
        pub set_stream_selection: unsafe extern "system" fn(*mut c_void, u32, i32) -> i32,
        pub get_native_media_type:
            unsafe extern "system" fn(*mut c_void, u32, u32, *mut *mut IMFMediaType) -> i32,
        pub get_current_media_type:
            unsafe extern "system" fn(*mut c_void, u32, *mut *mut IMFMediaType) -> i32,
        pub set_current_media_type:
            unsafe extern "system" fn(*mut c_void, u32, *mut u32, *mut IMFMediaType) -> i32,
        pub set_current_position: *const c_void,
        pub read_sample: unsafe extern "system" fn(
            *mut c_void,
            u32,
            u32,
            *mut u32,
            *mut u32,
            *mut i64,
            *mut *mut IMFSample,
        ) -> i32,
        pub flush: *const c_void,
        pub get_service_for_stream: *const c_void,
        pub get_presentation_attribute:
            unsafe extern "system" fn(*mut c_void, u32, *const GUID, *mut PROPVARIANT) -> i32,
    }

    #[repr(C)]
    pub struct IMFSourceReader {
        pub vtbl: *const IMFSourceReaderVtbl,
    }

    #[cfg(target_os = "windows")]
    #[link(name = "ole32")]
    extern "system" {
        fn CoInitializeEx(pv_reserved: *mut c_void, dw_co_init: u32) -> i32;
        fn PropVariantClear(pvar: *mut PROPVARIANT) -> i32;
    }

    #[cfg(target_os = "windows")]
    #[link(name = "mfplat")]
    extern "system" {
        fn MFStartup(version: u32, dw_flags: u32) -> i32;
        fn MFCreateMediaType(pp_mf_type: *mut *mut IMFMediaType) -> i32;
    }

    #[cfg(target_os = "windows")]
    #[link(name = "mfreadwrite")]
    extern "system" {
        fn MFCreateSourceReaderFromURL(
            pwsz_url: *const u16,
            p_attributes: *mut c_void,
            pp_source_reader: *mut *mut IMFSourceReader,
        ) -> i32;
    }

    #[cfg(not(target_os = "windows"))]
    unsafe fn CoInitializeEx(_pv_reserved: *mut c_void, _dw_co_init: u32) -> i32 {
        -1
    }
    #[cfg(not(target_os = "windows"))]
    unsafe fn PropVariantClear(_pvar: *mut PROPVARIANT) -> i32 {
        0
    }
    #[cfg(not(target_os = "windows"))]
    unsafe fn MFStartup(_version: u32, _dw_flags: u32) -> i32 {
        -1
    }
    #[cfg(not(target_os = "windows"))]
    unsafe fn MFCreateMediaType(_pp_mf_type: *mut *mut IMFMediaType) -> i32 {
        -1
    }
    #[cfg(not(target_os = "windows"))]
    unsafe fn MFCreateSourceReaderFromURL(
        _pwsz_url: *const u16,
        _p_attributes: *mut c_void,
        _pp_source_reader: *mut *mut IMFSourceReader,
    ) -> i32 {
        -1
    }

    fn ensure_mf_init() -> bool {
        unsafe {
            let _ = CoInitializeEx(null_mut(), COINIT_MULTITHREADED);
            MFStartup(MF_VERSION, MFSTARTUP_NOSOCKET) >= 0
        }
    }

    fn to_wide_path(path: &Path) -> Vec<u16> {
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::ffi::OsStrExt;
            path.as_os_str()
                .encode_wide()
                .chain(std::iter::once(0))
                .collect()
        }
        #[cfg(not(target_os = "windows"))]
        {
            path.to_string_lossy()
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect()
        }
    }

    pub fn probe_via_mf(path: &Path) -> Option<(f64, bool)> {
        if !ensure_mf_init() {
            return None;
        }
        unsafe {
            let wide = to_wide_path(path);
            let mut reader: *mut IMFSourceReader = null_mut();
            let hr = MFCreateSourceReaderFromURL(wide.as_ptr(), null_mut(), &mut reader);
            if hr < 0 || reader.is_null() {
                return None;
            }

            let mut native_audio: *mut IMFMediaType = null_mut();
            let audio_hr = ((*(*reader).vtbl).get_native_media_type)(
                reader as *mut c_void,
                MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                0,
                &mut native_audio,
            );
            let has_audio = audio_hr >= 0 && !native_audio.is_null();
            if !native_audio.is_null() {
                ((*(*native_audio).vtbl).release)(native_audio as *mut c_void);
            }

            let mut prop: PROPVARIANT = std::mem::zeroed();
            let dur_hr = ((*(*reader).vtbl).get_presentation_attribute)(
                reader as *mut c_void,
                MF_SOURCE_READER_MEDIASOURCE,
                &MF_PD_DURATION,
                &mut prop,
            );
            let mut dur_sec = 0.0f64;
            if dur_hr >= 0 && (prop.vt == 21 || prop.vt == 20) {
                let hns = prop.data[0];
                dur_sec = (hns as f64) / 10_000_000.0;
            }
            let _ = PropVariantClear(&mut prop);
            ((*(*reader).vtbl).release)(reader as *mut c_void);

            Some((dur_sec.max(0.0), has_audio))
        }
    }

    pub fn decode_via_mf(
        input_path: &Path,
        output_wav_path: &Path,
    ) -> Result<(f64, usize), SidecarError> {
        if !ensure_mf_init() {
            return Err(SidecarError::DecodeFailed(
                "Windows Media Foundation failed to initialize".to_string(),
            ));
        }

        unsafe {
            let wide = to_wide_path(input_path);
            let mut reader: *mut IMFSourceReader = null_mut();
            let hr = MFCreateSourceReaderFromURL(wide.as_ptr(), null_mut(), &mut reader);
            if hr < 0 || reader.is_null() {
                return Err(SidecarError::DecodeFailed(format!(
                    "Media Foundation could not open {} (HRESULT 0x{:08X})",
                    input_path.display(),
                    hr as u32
                )));
            }

            let mut native_audio: *mut IMFMediaType = null_mut();
            let audio_hr = ((*(*reader).vtbl).get_native_media_type)(
                reader as *mut c_void,
                MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                0,
                &mut native_audio,
            );
            if !native_audio.is_null() {
                ((*(*native_audio).vtbl).release)(native_audio as *mut c_void);
            }

            if audio_hr < 0 {
                let mut native_video: *mut IMFMediaType = null_mut();
                let video_hr = ((*(*reader).vtbl).get_native_media_type)(
                    reader as *mut c_void,
                    MF_SOURCE_READER_FIRST_VIDEO_STREAM,
                    0,
                    &mut native_video,
                );
                if !native_video.is_null() {
                    ((*(*native_video).vtbl).release)(native_video as *mut c_void);
                }
                ((*(*reader).vtbl).release)(reader as *mut c_void);
                if video_hr >= 0 {
                    return Err(SidecarError::NoAudioTrack(
                        "Video file contains no audio track".to_string(),
                    ));
                }
                return Err(SidecarError::DecodeFailed(
                    "Media container has no decodable audio stream".to_string(),
                ));
            }

            let _ = ((*(*reader).vtbl).set_stream_selection)(
                reader as *mut c_void,
                MF_SOURCE_READER_ALL_STREAMS,
                0,
            );
            let _ = ((*(*reader).vtbl).set_stream_selection)(
                reader as *mut c_void,
                MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                1,
            );

            let mut pcm_type: *mut IMFMediaType = null_mut();
            if MFCreateMediaType(&mut pcm_type) < 0 || pcm_type.is_null() {
                ((*(*reader).vtbl).release)(reader as *mut c_void);
                return Err(SidecarError::DecodeFailed(
                    "MFCreateMediaType failed".to_string(),
                ));
            }
            let _ = ((*(*pcm_type).vtbl).set_guid)(
                pcm_type as *mut c_void,
                &MF_MT_MAJOR_TYPE,
                &MF_MEDIA_TYPE_AUDIO,
            );
            let _ = ((*(*pcm_type).vtbl).set_guid)(
                pcm_type as *mut c_void,
                &MF_MT_SUBTYPE,
                &MF_AUDIO_FORMAT_PCM,
            );
            let _ = ((*(*pcm_type).vtbl).set_uint32)(
                pcm_type as *mut c_void,
                &MF_MT_AUDIO_BITS_PER_SAMPLE,
                16,
            );

            let set_hr = ((*(*reader).vtbl).set_current_media_type)(
                reader as *mut c_void,
                MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                null_mut(),
                pcm_type,
            );
            ((*(*pcm_type).vtbl).release)(pcm_type as *mut c_void);

            if set_hr < 0 {
                let mut flt_type: *mut IMFMediaType = null_mut();
                if MFCreateMediaType(&mut flt_type) >= 0 && !flt_type.is_null() {
                    let _ = ((*(*flt_type).vtbl).set_guid)(
                        flt_type as *mut c_void,
                        &MF_MT_MAJOR_TYPE,
                        &MF_MEDIA_TYPE_AUDIO,
                    );
                    let _ = ((*(*flt_type).vtbl).set_guid)(
                        flt_type as *mut c_void,
                        &MF_MT_SUBTYPE,
                        &MF_AUDIO_FORMAT_FLOAT,
                    );
                    let hr2 = ((*(*reader).vtbl).set_current_media_type)(
                        reader as *mut c_void,
                        MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                        null_mut(),
                        flt_type,
                    );
                    ((*(*flt_type).vtbl).release)(flt_type as *mut c_void);
                    if hr2 < 0 {
                        ((*(*reader).vtbl).release)(reader as *mut c_void);
                        return Err(SidecarError::DecodeFailed(
                            "Media Foundation could not negotiate PCM/Float output".to_string(),
                        ));
                    }
                }
            }

            let mut cur_type: *mut IMFMediaType = null_mut();
            let mut out_sr: u32 = 16000;
            let mut out_ch: u32 = 1;
            let mut out_bps: u32 = 16;
            let mut is_float = false;

            if ((*(*reader).vtbl).get_current_media_type)(
                reader as *mut c_void,
                MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                &mut cur_type,
            ) >= 0
                && !cur_type.is_null()
            {
                let _ = ((*(*cur_type).vtbl).get_uint32)(
                    cur_type as *mut c_void,
                    &MF_MT_AUDIO_SAMPLES_PER_SECOND,
                    &mut out_sr,
                );
                let _ = ((*(*cur_type).vtbl).get_uint32)(
                    cur_type as *mut c_void,
                    &MF_MT_AUDIO_NUM_CHANNELS,
                    &mut out_ch,
                );
                let _ = ((*(*cur_type).vtbl).get_uint32)(
                    cur_type as *mut c_void,
                    &MF_MT_AUDIO_BITS_PER_SAMPLE,
                    &mut out_bps,
                );
                let mut subtype: GUID = std::mem::zeroed();
                if ((*(*cur_type).vtbl).get_guid)(
                    cur_type as *mut c_void,
                    &MF_MT_SUBTYPE,
                    &mut subtype,
                ) >= 0
                    && subtype == MF_AUDIO_FORMAT_FLOAT
                {
                    is_float = true;
                }
                ((*(*cur_type).vtbl).release)(cur_type as *mut c_void);
            }

            out_ch = out_ch.max(1);
            out_sr = out_sr.max(8000);

            let mut mono_samples: Vec<f32> = Vec::new();

            loop {
                let mut actual_stream: u32 = 0;
                let mut stream_flags: u32 = 0;
                let mut timestamp: i64 = 0;
                let mut sample: *mut IMFSample = null_mut();

                let read_hr = ((*(*reader).vtbl).read_sample)(
                    reader as *mut c_void,
                    MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                    0,
                    &mut actual_stream,
                    &mut stream_flags,
                    &mut timestamp,
                    &mut sample,
                );

                if read_hr < 0 || (stream_flags & MF_SOURCE_READERF_ERROR) != 0 {
                    if !sample.is_null() {
                        ((*(*sample).vtbl).release)(sample as *mut c_void);
                    }
                    break;
                }

                if (stream_flags & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED) != 0 {
                    let mut new_type: *mut IMFMediaType = null_mut();
                    if ((*(*reader).vtbl).get_current_media_type)(
                        reader as *mut c_void,
                        MF_SOURCE_READER_FIRST_AUDIO_STREAM,
                        &mut new_type,
                    ) >= 0
                        && !new_type.is_null()
                    {
                        let _ = ((*(*new_type).vtbl).get_uint32)(
                            new_type as *mut c_void,
                            &MF_MT_AUDIO_SAMPLES_PER_SECOND,
                            &mut out_sr,
                        );
                        let _ = ((*(*new_type).vtbl).get_uint32)(
                            new_type as *mut c_void,
                            &MF_MT_AUDIO_NUM_CHANNELS,
                            &mut out_ch,
                        );
                        let _ = ((*(*new_type).vtbl).get_uint32)(
                            new_type as *mut c_void,
                            &MF_MT_AUDIO_BITS_PER_SAMPLE,
                            &mut out_bps,
                        );
                        ((*(*new_type).vtbl).release)(new_type as *mut c_void);
                    }
                }

                if !sample.is_null() {
                    let mut media_buf: *mut IMFMediaBuffer = null_mut();
                    if ((*(*sample).vtbl).convert_to_contiguous_buffer)(
                        sample as *mut c_void,
                        &mut media_buf,
                    ) >= 0
                        && !media_buf.is_null()
                    {
                        let mut ptr: *mut u8 = null_mut();
                        let mut max_len: u32 = 0;
                        let mut cur_len: u32 = 0;
                        if ((*(*media_buf).vtbl).lock)(
                            media_buf as *mut c_void,
                            &mut ptr,
                            &mut max_len,
                            &mut cur_len,
                        ) >= 0
                            && !ptr.is_null()
                            && cur_len > 0
                        {
                            let slice = std::slice::from_raw_parts(ptr, cur_len as usize);
                            let ch = (out_ch as usize).max(1);
                            let inv_ch = 1.0f32 / (ch as f32);

                            if is_float || out_bps == 32 {
                                let frame_bytes = ch * 4;
                                for frame in slice.chunks_exact(frame_bytes) {
                                    let mut sum = 0.0f32;
                                    for c in 0..ch {
                                        let b = &frame[c * 4..(c + 1) * 4];
                                        let v = if is_float {
                                            f32::from_le_bytes([b[0], b[1], b[2], b[3]])
                                        } else {
                                            (i32::from_le_bytes([b[0], b[1], b[2], b[3]]) as f32)
                                                / 2_147_483_648.0
                                        };
                                        sum += v;
                                    }
                                    mono_samples.push(sum * inv_ch);
                                }
                            } else {
                                let frame_bytes = ch * 2;
                                for frame in slice.chunks_exact(frame_bytes) {
                                    let mut sum = 0.0f32;
                                    for c in 0..ch {
                                        let b = &frame[c * 2..(c + 1) * 2];
                                        let v =
                                            (i16::from_le_bytes([b[0], b[1]]) as f32) / 32768.0;
                                        sum += v;
                                    }
                                    mono_samples.push(sum * inv_ch);
                                }
                            }

                            let _ = ((*(*media_buf).vtbl).unlock)(media_buf as *mut c_void);
                        }
                        ((*(*media_buf).vtbl).release)(media_buf as *mut c_void);
                    }
                    ((*(*sample).vtbl).release)(sample as *mut c_void);
                }

                if (stream_flags & MF_SOURCE_READERF_ENDOFSTREAM) != 0 {
                    break;
                }
            }

            ((*(*reader).vtbl).release)(reader as *mut c_void);

            let real_duration = (mono_samples.len() as f64) / (out_sr as f64);
            let resampled = resample_mono_f32(&mono_samples, out_sr, 16000);
            let mut writer = WavStreamWriter::create(output_wav_path, 16000)?;
            writer.append_f32_samples(&resampled)?;
            writer.pad_to_minimum_samples(16000)?;
            let final_samples = writer.sample_count();
            writer.finish()?;

            Ok((real_duration, final_samples))
        }
    }
}

pub fn win_mf_probe(path: &Path) -> Option<(f64, bool)> {
    win_mf::probe_via_mf(path)
}

// MARK: - 3. FFmpeg / afconvert Fallback Decoder

fn check_video_without_audio(input_path: &Path) -> Result<(), SidecarError> {
    if let Ok(mut file) = File::open(input_path) {
        if let Some((_, has_audio)) = probe_mp4(&mut file) {
            if !has_audio {
                return Err(SidecarError::NoAudioTrack(
                    "Video file contains no audio track".to_string(),
                ));
            }
        }
        if let Some((_, has_audio)) = probe_matroska(&mut file) {
            if !has_audio {
                return Err(SidecarError::NoAudioTrack(
                    "Video file contains no audio track".to_string(),
                ));
            }
        }
    }
    if let Some((_, has_audio)) = probe_with_ffprobe(input_path) {
        if !has_audio {
            return Err(SidecarError::NoAudioTrack(
                "Media file contains no audio track".to_string(),
            ));
        }
    }
    Ok(())
}

fn decode_via_ffmpeg(
    input_path: &Path,
    output_wav_path: &Path,
) -> Result<(f64, usize), SidecarError> {
    check_video_without_audio(input_path)?;

    if let Some(ffmpeg) = find_runtime_or_system_exe("ffmpeg") {
        let raw_tmp = output_wav_path.with_extension("ffmpeg.wav");
        let mut cmd = Command::new(ffmpeg);
        cmd.args([
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
        ])
        .arg(input_path)
        .args(["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"])
        .arg(&raw_tmp);
        apply_no_window(&mut cmd);

        let out = cmd
            .output()
            .map_err(|e| SidecarError::DecodeFailed(format!("Failed to launch ffmpeg: {}", e)))?;
        if !out.status.success() {
            let _ = std::fs::remove_file(&raw_tmp);
            let err_text = String::from_utf8_lossy(&out.stderr).trim().to_string();
            let lower = err_text.to_ascii_lowercase();
            if lower.contains("does not contain any stream")
                || lower.contains("matches no streams")
                || lower.contains("output file #0 does not contain")
            {
                return Err(SidecarError::NoAudioTrack(
                    "Media file contains no audio track".to_string(),
                ));
            }
            return Err(SidecarError::DecodeFailed(format!(
                "Audio decode failed: {}",
                if err_text.is_empty() {
                    "ffmpeg returned non-zero status"
                } else {
                    &err_text
                }
            )));
        }

        let res = decode_native_wav(&raw_tmp, output_wav_path);
        let _ = std::fs::remove_file(&raw_tmp);
        return res;
    }

    // macOS developer/test fallback via system /usr/bin/afconvert
    #[cfg(target_os = "macos")]
    {
        if Path::new("/usr/bin/afconvert").is_file() {
            let raw_tmp = output_wav_path.with_extension("afconvert.wav");
            let status = Command::new("/usr/bin/afconvert")
                .args(["-f", "WAVE", "-d", "LEI16@16000", "-c", "1"])
                .arg(input_path)
                .arg(&raw_tmp)
                .status();
            if let Ok(st) = status {
                if st.success() {
                    let res = decode_native_wav(&raw_tmp, output_wav_path);
                    let _ = std::fs::remove_file(&raw_tmp);
                    return res;
                }
            }
            let _ = std::fs::remove_file(&raw_tmp);
        }
    }

    let ext = input_path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("media");
    Err(SidecarError::DecodeFailed(format!(
        "Could not decode .{} directly and ffmpeg was not found in audio_runtime or PATH.",
        ext
    )))
}

pub fn prepare_normalized_wav(input_path: &Path) -> Result<NormalizedWav, SidecarError> {
    let seq = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let temp_wav =
        std::env::temp_dir().join(format!("docprep-audio-{}-{}.wav", std::process::id(), seq));

    // Check upfront if container is known video-only (MP4/MOV/MKV/WebM without audio track)
    check_video_without_audio(input_path)?;

    let ext = input_path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();

    // 1. Fast native WAV decoder (handles 8/16/24/32-bit PCM & 32/64-bit Float WAV in-process)
    if ext == "wav" {
        if let Ok((dur, count)) = decode_native_wav(input_path, &temp_wav) {
            return Ok(NormalizedWav {
                wav_path: temp_wav,
                duration_sec: dur,
                sample_count: count,
            });
        }
    }

    // 2. Windows Media Foundation hardware/OS streaming decoder
    #[cfg(target_os = "windows")]
    {
        match win_mf::decode_via_mf(input_path, &temp_wav) {
            Ok((dur, count)) => {
                return Ok(NormalizedWav {
                    wav_path: temp_wav,
                    duration_sec: dur,
                    sample_count: count,
                });
            }
            Err(SidecarError::NoAudioTrack(msg)) => {
                let _ = std::fs::remove_file(&temp_wav);
                return Err(SidecarError::NoAudioTrack(msg));
            }
            Err(_) => {
                // Fall through to ffmpeg / native WAV fallback
            }
        }
    }

    // 3. Also try native WAV parser if file has WAV magic regardless of extension
    if let Ok((dur, count)) = decode_native_wav(input_path, &temp_wav) {
        return Ok(NormalizedWav {
            wav_path: temp_wav,
            duration_sec: dur,
            sample_count: count,
        });
    }

    // 4. FFmpeg / platform fallback
    match decode_via_ffmpeg(input_path, &temp_wav) {
        Ok((dur, count)) => Ok(NormalizedWav {
            wav_path: temp_wav,
            duration_sec: dur,
            sample_count: count,
        }),
        Err(e) => {
            let _ = std::fs::remove_file(&temp_wav);
            Err(e)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_wav_writer_and_resampler_roundtrip() {
        let tmp = std::env::temp_dir().join(format!("docprep-test-wav-{}.wav", std::process::id()));
        let mut writer48 = WavStreamWriter::create(&tmp, 48000).unwrap();
        let sine: Vec<f32> = (0..12000)
            .map(|i| ((i as f32) * 440.0 * 2.0 * std::f32::consts::PI / 48000.0).sin() * 0.5)
            .collect();
        writer48.append_f32_samples(&sine).unwrap();
        writer48.finish().unwrap();

        let norm = prepare_normalized_wav(&tmp).unwrap();
        let _ = std::fs::remove_file(&tmp);
        assert!((norm.duration_sec - 0.25).abs() < 0.01);
        assert_eq!(
            norm.sample_count, 16000,
            "clips < 1s are padded to 16000 samples"
        );
        let loaded = load_normalized_wav_f32(&norm.wav_path).unwrap();
        assert_eq!(loaded.len(), 16000);
    }
}
