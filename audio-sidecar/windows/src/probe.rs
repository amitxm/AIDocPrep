use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::process::Command;

use crate::json::parse_json;
use crate::transcribe::{apply_no_window, find_runtime_or_system_exe};

#[derive(Debug, Clone, PartialEq)]
pub struct ProbeEntry {
    pub path: String,
    pub duration_sec: f64,
    pub has_audio: bool,
}

fn round_tenth(val: f64) -> f64 {
    if !val.is_finite() || val < 0.0 {
        0.0
    } else {
        (val * 10.0).round() / 10.0
    }
}

// MARK: - 1. RIFF / WAV Probe

pub fn probe_wav(file: &mut File) -> Option<(f64, bool)> {
    let _ = file.seek(SeekFrom::Start(0));
    let mut riff_hdr = [0u8; 12];
    if file.read_exact(&mut riff_hdr).is_err() {
        return None;
    }
    if (&riff_hdr[0..4] != b"RIFF" && &riff_hdr[0..4] != b"RF64") || &riff_hdr[8..12] != b"WAVE" {
        return None;
    }

    let file_len = file.seek(SeekFrom::End(0)).ok()?;
    let _ = file.seek(SeekFrom::Start(12));

    let mut byte_rate: u32 = 0;
    let mut sample_rate: u32 = 0;
    let mut channels: u16 = 0;
    let mut bits_per_sample: u16 = 0;
    let mut data_bytes: u64 = 0;
    let mut rf64_data_size: Option<u64> = None;

    while let Ok(pos) = file.stream_position() {
        if pos + 8 > file_len {
            break;
        }
        let mut chunk_hdr = [0u8; 8];
        if file.read_exact(&mut chunk_hdr).is_err() {
            break;
        }
        let chunk_id = &chunk_hdr[0..4];
        let chunk_size = u32::from_le_bytes([chunk_hdr[4], chunk_hdr[5], chunk_hdr[6], chunk_hdr[7]]) as u64;

        if chunk_id == b"ds64" && chunk_size >= 16 {
            let mut ds64_buf = [0u8; 16];
            if file.read_exact(&mut ds64_buf).is_ok() {
                let ds_size = u64::from_le_bytes(ds64_buf[8..16].try_into().unwrap());
                rf64_data_size = Some(ds_size);
            }
        } else if chunk_id == b"fmt " && chunk_size >= 16 {
            let mut fmt_buf = [0u8; 16];
            if file.read_exact(&mut fmt_buf).is_ok() {
                channels = u16::from_le_bytes([fmt_buf[2], fmt_buf[3]]);
                sample_rate = u32::from_le_bytes([fmt_buf[4], fmt_buf[5], fmt_buf[6], fmt_buf[7]]);
                byte_rate = u32::from_le_bytes([fmt_buf[8], fmt_buf[9], fmt_buf[10], fmt_buf[11]]);
                bits_per_sample = u16::from_le_bytes([fmt_buf[14], fmt_buf[15]]);
            }
        } else if chunk_id == b"data" {
            data_bytes = if chunk_size == 0xFFFF_FFFF {
                rf64_data_size.unwrap_or_else(|| file_len.saturating_sub(pos + 8))
            } else {
                chunk_size.min(file_len.saturating_sub(pos + 8))
            };
            break;
        }

        let padded = chunk_size + (chunk_size & 1);
        let next = pos.saturating_add(8).saturating_add(padded);
        if next >= file_len || file.seek(SeekFrom::Start(next)).is_err() {
            break;
        }
    }

    let effective_byte_rate = if byte_rate > 0 {
        byte_rate as f64
    } else if sample_rate > 0 && channels > 0 && bits_per_sample > 0 {
        (sample_rate as f64) * (channels as f64) * ((bits_per_sample as f64) / 8.0)
    } else {
        0.0
    };

    if effective_byte_rate <= 0.0 {
        return Some((0.0, false));
    }

    let dur = (data_bytes as f64) / effective_byte_rate;
    Some((dur.max(0.0), data_bytes > 0))
}

// MARK: - 2. FLAC Probe

pub fn probe_flac(file: &mut File) -> Option<(f64, bool)> {
    let _ = file.seek(SeekFrom::Start(0));
    let mut buf = [0u8; 42];
    if file.read_exact(&mut buf).is_err() {
        return None;
    }
    if &buf[0..4] != b"fLaC" {
        return None;
    }
    let block_type = buf[4] & 0x7F;
    if block_type != 0 {
        // STREAMINFO must be block 0
        return Some((0.0, true));
    }
    // STREAMINFO starts at byte 8 (34 bytes long)
    // Bytes 18..26 (relative to file start) hold:
    //   20 bits sample rate, 3 bits (channels-1), 5 bits (bps-1), 36 bits total samples
    let b18 = buf[18] as u64;
    let b19 = buf[19] as u64;
    let b20 = buf[20] as u64;
    let sample_rate = (b18 << 12) | (b19 << 4) | (b20 >> 4);

    let b21 = (buf[21] & 0x0F) as u64;
    let tail_32 = u32::from_be_bytes([buf[22], buf[23], buf[24], buf[25]]) as u64;
    let total_samples = (b21 << 32) | tail_32;

    if sample_rate == 0 {
        return Some((0.0, false));
    }
    let dur = (total_samples as f64) / (sample_rate as f64);
    Some((dur.max(0.0), total_samples > 0))
}

// MARK: - 3. Ogg Opus / Ogg Vorbis Probe

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || needle.len() > haystack.len() {
        return None;
    }
    haystack.windows(needle.len()).position(|w| w == needle)
}

pub fn probe_ogg(file: &mut File) -> Option<(f64, bool)> {
    let _ = file.seek(SeekFrom::Start(0));
    let mut head = vec![0u8; 8192];
    let n = file.read(&mut head).ok()?;
    head.truncate(n);
    if head.len() < 36 || &head[0..4] != b"OggS" {
        return None;
    }

    let mut sample_rate = 0.0f64;
    let mut pre_skip: i64 = 0;

    if let Some(opus_pos) = find_subslice(&head, b"OpusHead") {
        sample_rate = 48000.0; // Ogg Opus granule positions are always 48 kHz
        if opus_pos + 12 <= head.len() {
            pre_skip = u16::from_le_bytes([head[opus_pos + 10], head[opus_pos + 11]]) as i64;
        }
    } else if let Some(vorbis_pos) = find_subslice(&head, b"\x01vorbis") {
        if vorbis_pos + 16 <= head.len() {
            let sr = u32::from_le_bytes([
                head[vorbis_pos + 12],
                head[vorbis_pos + 13],
                head[vorbis_pos + 14],
                head[vorbis_pos + 15],
            ]);
            if sr > 0 {
                sample_rate = sr as f64;
            }
        }
    }

    if sample_rate <= 0.0 {
        return Some((0.0, true));
    }

    let file_size = file.seek(SeekFrom::End(0)).unwrap_or(head.len() as u64);
    let tail_len = file_size.min(65536) as usize;
    let _ = file.seek(SeekFrom::Start(file_size - tail_len as u64));
    let mut tail = vec![0u8; tail_len];
    let tn = file.read(&mut tail).ok()?;
    tail.truncate(tn);

    let mut last_granule: i64 = 0;
    let mut idx = 0;
    while idx + 14 <= tail.len() {
        if &tail[idx..idx + 4] == b"OggS" {
            let gran = i64::from_le_bytes(tail[idx + 6..idx + 14].try_into().unwrap());
            if gran > last_granule {
                last_granule = gran;
            }
            idx += 4;
        } else {
            idx += 1;
        }
    }

    let samples = (last_granule - pre_skip).max(0) as f64;
    Some(((samples / sample_rate).max(0.0), true))
}

// MARK: - 4. Matroska / WebM EBML Probe

fn read_ebml_vint(data: &[u8], idx: &mut usize, mask_length: bool) -> Option<u64> {
    if *idx >= data.len() {
        return None;
    }
    let first = data[*idx];
    if first == 0 {
        return None;
    }
    let mut length = 1usize;
    let mut mask = 0x80u8;
    while (first & mask) == 0 {
        length += 1;
        mask >>= 1;
        if length > 8 {
            return None;
        }
    }
    if *idx + length > data.len() {
        return None;
    }
    let mut value = if mask_length {
        (first & (mask - 1)) as u64
    } else {
        first as u64
    };
    for i in 1..length {
        value = (value << 8) | (data[*idx + i] as u64);
    }
    *idx += length;
    Some(value)
}

pub fn probe_matroska(file: &mut File) -> Option<(f64, bool)> {
    let _ = file.seek(SeekFrom::Start(0));
    let mut data = vec![0u8; 262_144];
    let n = file.read(&mut data).ok()?;
    data.truncate(n);
    if data.len() < 4 || data[0..4] != [0x1A, 0x45, 0xDF, 0xA3] {
        return None;
    }

    let mut idx = 0usize;
    let mut timestamp_scale = 1_000_000.0f64;
    let mut raw_duration: Option<f64> = None;
    let mut has_audio = false;

    while idx < data.len() {
        let start_idx = idx;
        let (elem_id, elem_size) = match (
            read_ebml_vint(&data, &mut idx, false),
            read_ebml_vint(&data, &mut idx, true),
        ) {
            (Some(id), Some(sz)) => (id, sz),
            _ => {
                idx = start_idx + 1;
                continue;
            }
        };

        // Stop at Cluster (0x1F43B675)
        if elem_id == 0x1F43_B675 {
            break;
        }

        // Master elements to descend into: EBML, Segment, Info, Tracks, TrackEntry
        if matches!(
            elem_id,
            0x1A45_DFA3 | 0x1853_8067 | 0x1549_A966 | 0x1654_AE6B | 0xAE
        ) {
            continue;
        }

        let rem = data.len().saturating_sub(idx);
        let size = (elem_size.min(rem as u64)) as usize;

        if elem_id == 0x2AD7_B1 && (1..=8).contains(&size) {
            let mut v = 0u64;
            for i in 0..size {
                v = (v << 8) | (data[idx + i] as u64);
            }
            if v > 0 {
                timestamp_scale = v as f64;
            }
        } else if elem_id == 0x4489 && (size == 4 || size == 8) {
            if size == 4 {
                let bits = u32::from_be_bytes(data[idx..idx + 4].try_into().unwrap());
                raw_duration = Some(f32::from_bits(bits) as f64);
            } else {
                let bits = u64::from_be_bytes(data[idx..idx + 8].try_into().unwrap());
                raw_duration = Some(f64::from_bits(bits));
            }
        } else if elem_id == 0x83 && size == 1 {
            if data[idx] == 2 {
                has_audio = true;
            }
        }

        if elem_size >= 0x00FF_FFFF_FFFF_FFFF {
            break;
        }
        idx = idx.saturating_add(size);
    }

    let dur = raw_duration.unwrap_or(0.0) * timestamp_scale / 1_000_000_000.0;
    Some((dur.max(0.0), has_audio))
}

// MARK: - 5. ISO BMFF / MP4 / MOV / M4A Atom Probe

struct Mp4ProbeState {
    movie_duration_sec: f64,
    audio_duration_sec: f64,
    has_audio_track: bool,
    has_any_track: bool,
    current_track_mdhd_sec: f64,
    current_track_is_audio: bool,
}

fn parse_mp4_boxes(file: &mut File, start: u64, end: u64, state: &mut Mp4ProbeState, depth: u32) {
    if depth > 12 {
        return;
    }
    let mut pos = start;
    while pos + 8 <= end {
        if file.seek(SeekFrom::Start(pos)).is_err() {
            break;
        }
        let mut hdr = [0u8; 8];
        if file.read_exact(&mut hdr).is_err() {
            break;
        }
        let size32 = u32::from_be_bytes([hdr[0], hdr[1], hdr[2], hdr[3]]) as u64;
        let box_type = &hdr[4..8];

        let (box_size, header_len) = if size32 == 1 {
            let mut ext = [0u8; 8];
            if file.read_exact(&mut ext).is_err() {
                break;
            }
            (u64::from_be_bytes(ext), 16u64)
        } else if size32 == 0 {
            (end.saturating_sub(pos), 8u64)
        } else {
            (size32, 8u64)
        };

        if box_size < header_len {
            break;
        }
        let box_end = pos.saturating_add(box_size).min(end);
        let payload_start = pos + header_len;
        let payload_len = box_end.saturating_sub(payload_start);

        match box_type {
            b"moov" | b"mdia" => {
                parse_mp4_boxes(file, payload_start, box_end, state, depth + 1);
            }
            b"trak" => {
                state.current_track_mdhd_sec = 0.0;
                state.current_track_is_audio = false;
                state.has_any_track = true;
                parse_mp4_boxes(file, payload_start, box_end, state, depth + 1);
                if state.current_track_is_audio {
                    state.has_audio_track = true;
                    if state.current_track_mdhd_sec > state.audio_duration_sec {
                        state.audio_duration_sec = state.current_track_mdhd_sec;
                    }
                }
            }
            b"mvhd" if payload_len >= 20 => {
                let mut buf = [0u8; 32];
                let to_read = (payload_len as usize).min(buf.len());
                if file.read_exact(&mut buf[..to_read]).is_ok() {
                    let version = buf[0];
                    if version == 1 && to_read >= 32 {
                        let ts = u32::from_be_bytes([buf[20], buf[21], buf[22], buf[23]]) as f64;
                        let dur = u64::from_be_bytes(buf[24..32].try_into().unwrap()) as f64;
                        if ts > 0.0 && dur < (u64::MAX as f64) {
                            state.movie_duration_sec = dur / ts;
                        }
                    } else if version == 0 && to_read >= 20 {
                        let ts = u32::from_be_bytes([buf[12], buf[13], buf[14], buf[15]]) as f64;
                        let dur = u32::from_be_bytes([buf[16], buf[17], buf[18], buf[19]]) as f64;
                        if ts > 0.0 && dur < (u32::MAX as f64) {
                            state.movie_duration_sec = dur / ts;
                        }
                    }
                }
            }
            b"mdhd" if payload_len >= 20 => {
                let mut buf = [0u8; 32];
                let to_read = (payload_len as usize).min(buf.len());
                if file.read_exact(&mut buf[..to_read]).is_ok() {
                    let version = buf[0];
                    if version == 1 && to_read >= 32 {
                        let ts = u32::from_be_bytes([buf[20], buf[21], buf[22], buf[23]]) as f64;
                        let dur = u64::from_be_bytes(buf[24..32].try_into().unwrap()) as f64;
                        if ts > 0.0 && dur < (u64::MAX as f64) {
                            state.current_track_mdhd_sec = dur / ts;
                        }
                    } else if version == 0 && to_read >= 20 {
                        let ts = u32::from_be_bytes([buf[12], buf[13], buf[14], buf[15]]) as f64;
                        let dur = u32::from_be_bytes([buf[16], buf[17], buf[18], buf[19]]) as f64;
                        if ts > 0.0 && dur < (u32::MAX as f64) {
                            state.current_track_mdhd_sec = dur / ts;
                        }
                    }
                }
            }
            b"hdlr" if payload_len >= 12 => {
                let mut buf = [0u8; 12];
                if file.read_exact(&mut buf).is_ok() {
                    if &buf[8..12] == b"soun" {
                        state.current_track_is_audio = true;
                    }
                }
            }
            _ => {}
        }

        pos = box_end;
    }
}

pub fn probe_mp4(file: &mut File) -> Option<(f64, bool)> {
    let file_len = file.seek(SeekFrom::End(0)).ok()?;
    if file_len < 16 {
        return None;
    }
    let _ = file.seek(SeekFrom::Start(0));
    let mut head = [0u8; 12];
    if file.read_exact(&mut head).is_err() {
        return None;
    }
    let box_type = &head[4..8];
    if !matches!(
        box_type,
        b"ftyp" | b"moov" | b"mdat" | b"wide" | b"free" | b"skip" | b"pnot"
    ) {
        return None;
    }

    let mut state = Mp4ProbeState {
        movie_duration_sec: 0.0,
        audio_duration_sec: 0.0,
        has_audio_track: false,
        has_any_track: false,
        current_track_mdhd_sec: 0.0,
        current_track_is_audio: false,
    };

    parse_mp4_boxes(file, 0, file_len, &mut state, 0);

    if !state.has_any_track && state.movie_duration_sec <= 0.0 {
        return None;
    }

    let dur = if state.audio_duration_sec > 0.0 {
        state.audio_duration_sec
    } else {
        state.movie_duration_sec
    };
    Some((dur.max(0.0), state.has_audio_track))
}

// MARK: - 6. MP3 & ADTS AAC Probe

pub fn probe_mp3_or_aac(file: &mut File) -> Option<(f64, bool)> {
    let file_len = file.seek(SeekFrom::End(0)).ok()?;
    if file_len < 16 {
        return None;
    }
    let _ = file.seek(SeekFrom::Start(0));
    let mut head = [0u8; 10];
    if file.read_exact(&mut head).is_err() {
        return None;
    }

    let mut audio_offset = 0u64;
    if &head[0..3] == b"ID3" {
        let sz = ((head[6] as u64 & 0x7F) << 21)
            | ((head[7] as u64 & 0x7F) << 14)
            | ((head[8] as u64 & 0x7F) << 7)
            | (head[9] as u64 & 0x7F);
        audio_offset = 10 + sz;
    }

    if audio_offset >= file_len {
        return None;
    }
    let _ = file.seek(SeekFrom::Start(audio_offset));
    let mut scan = vec![0u8; 8192.min((file_len - audio_offset) as usize)];
    let n = file.read(&mut scan).ok()?;
    scan.truncate(n);

    for i in 0..scan.len().saturating_sub(4) {
        if scan[i] == 0xFF && (scan[i + 1] & 0xE0) == 0xE0 {
            let b1 = scan[i + 1];
            let b2 = scan[i + 2];
            let b3 = scan[i + 3];

            let version_bits = (b1 >> 3) & 0x03;
            let layer_bits = (b1 >> 1) & 0x03;

            // ADTS AAC frame: layer_bits == 0
            if layer_bits == 0 && version_bits >= 2 {
                return Some((0.0, true));
            }

            // MPEG Audio Layer III (layer_bits == 1)
            if layer_bits == 1 && version_bits != 1 {
                let bitrate_idx = ((b2 >> 4) & 0x0F) as usize;
                let sr_idx = ((b2 >> 2) & 0x03) as usize;
                if bitrate_idx == 0 || bitrate_idx == 15 || sr_idx == 3 {
                    continue;
                }

                let sample_rate = match version_bits {
                    3 => [44100u32, 48000, 32000][sr_idx], // MPEG-1
                    2 => [22050u32, 24000, 16000][sr_idx], // MPEG-2
                    0 => [11025u32, 12000, 8000][sr_idx],  // MPEG-2.5
                    _ => continue,
                };

                let kbps = if version_bits == 3 {
                    [0u32, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320][bitrate_idx]
                } else {
                    [0u32, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160][bitrate_idx]
                };

                let samples_per_frame = if version_bits == 3 { 1152u32 } else { 576u32 };
                let channel_mode = (b3 >> 6) & 0x03;
                let side_info_len = if version_bits == 3 {
                    if channel_mode == 3 { 17usize } else { 32usize }
                } else if channel_mode == 3 {
                    9usize
                } else {
                    17usize
                };

                // Check for Xing / Info VBR frame count header
                let xing_pos = i + 4 + side_info_len;
                if xing_pos + 12 <= scan.len() {
                    let tag = &scan[xing_pos..xing_pos + 4];
                    if tag == b"Xing" || tag == b"Info" {
                        let flags = u32::from_be_bytes(scan[xing_pos + 4..xing_pos + 8].try_into().unwrap());
                        if (flags & 1) != 0 {
                            let total_frames =
                                u32::from_be_bytes(scan[xing_pos + 8..xing_pos + 12].try_into().unwrap());
                            let dur = (total_frames as f64) * (samples_per_frame as f64) / (sample_rate as f64);
                            return Some((dur.max(0.0), true));
                        }
                    }
                }

                // CBR estimate from file payload size
                if kbps > 0 {
                    let payload_bytes = file_len.saturating_sub(audio_offset + i as u64) as f64;
                    let dur = (payload_bytes * 8.0) / ((kbps as f64) * 1000.0);
                    return Some((dur.max(0.0), true));
                }
            }
        }
    }

    None
}

// MARK: - 7. ffprobe Fallback

pub fn probe_with_ffprobe(path: &Path) -> Option<(f64, bool)> {
    let ffprobe = find_runtime_or_system_exe("ffprobe")?;
    let mut cmd = Command::new(ffprobe);
    cmd.args([
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type:format=duration",
        "-of",
        "json",
    ])
    .arg(path);
    apply_no_window(&mut cmd);

    let out = cmd.output().ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let parsed = parse_json(&text).ok()?;
    let has_audio = parsed
        .get("streams")
        .and_then(|s| s.as_array())
        .map_or(false, |arr| !arr.is_empty());
    let dur = parsed
        .get("format")
        .and_then(|f| f.get("duration"))
        .and_then(|d| d.as_f64())
        .unwrap_or(0.0);
    Some((dur.max(0.0), has_audio))
}

pub fn probe_single_file(path_str: &str) -> ProbeEntry {
    let path = Path::new(path_str);
    if !path.is_file() {
        return ProbeEntry {
            path: path_str.to_string(),
            duration_sec: 0.0,
            has_audio: false,
        };
    }

    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();

    if let Ok(mut file) = File::open(path) {
        // 1. RIFF/WAV
        if let Some((dur, has_audio)) = probe_wav(&mut file) {
            return ProbeEntry {
                path: path_str.to_string(),
                duration_sec: round_tenth(dur),
                has_audio,
            };
        }

        // 2. FLAC
        if let Some((dur, has_audio)) = probe_flac(&mut file) {
            return ProbeEntry {
                path: path_str.to_string(),
                duration_sec: round_tenth(dur),
                has_audio,
            };
        }

        // 3. Ogg Opus / Vorbis
        if let Some((dur, has_audio)) = probe_ogg(&mut file) {
            return ProbeEntry {
                path: path_str.to_string(),
                duration_sec: round_tenth(dur),
                has_audio,
            };
        }

        // 4. Matroska / WebM
        if let Some((dur, has_audio)) = probe_matroska(&mut file) {
            if dur > 0.0 || !has_audio {
                return ProbeEntry {
                    path: path_str.to_string(),
                    duration_sec: round_tenth(dur),
                    has_audio,
                };
            }
            #[cfg(target_os = "windows")]
            if let Some((mf_dur, mf_audio)) = crate::decode::win_mf_probe(path) {
                return ProbeEntry {
                    path: path_str.to_string(),
                    duration_sec: round_tenth(mf_dur),
                    has_audio: mf_audio,
                };
            }
            if let Some((ff_dur, ff_audio)) = probe_with_ffprobe(path) {
                return ProbeEntry {
                    path: path_str.to_string(),
                    duration_sec: round_tenth(ff_dur),
                    has_audio: ff_audio,
                };
            }
            return ProbeEntry {
                path: path_str.to_string(),
                duration_sec: 0.0,
                has_audio,
            };
        }

        // 5. ISO BMFF / MP4 / MOV / M4A
        if let Some((dur, has_audio)) = probe_mp4(&mut file) {
            return ProbeEntry {
                path: path_str.to_string(),
                duration_sec: round_tenth(dur),
                has_audio,
            };
        }

        // 6. MP3 / AAC
        if matches!(ext.as_str(), "mp3" | "aac") {
            if let Some((dur, has_audio)) = probe_mp3_or_aac(&mut file) {
                if dur > 0.0 {
                    return ProbeEntry {
                        path: path_str.to_string(),
                        duration_sec: round_tenth(dur),
                        has_audio,
                    };
                }
            }
        }
    }

    // 7. Windows Media Foundation probe fallback
    #[cfg(target_os = "windows")]
    if let Some((mf_dur, mf_audio)) = crate::decode::win_mf_probe(path) {
        return ProbeEntry {
            path: path_str.to_string(),
            duration_sec: round_tenth(mf_dur),
            has_audio: mf_audio,
        };
    }

    // 8. ffprobe fallback
    if let Some((ff_dur, ff_audio)) = probe_with_ffprobe(path) {
        return ProbeEntry {
            path: path_str.to_string(),
            duration_sec: round_tenth(ff_dur),
            has_audio: ff_audio,
        };
    }

    ProbeEntry {
        path: path_str.to_string(),
        duration_sec: 0.0,
        has_audio: false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn test_probe_mp4_audio_vs_video_only() {
        let tmp = std::env::temp_dir().join(format!("docprep-test-mp4-{}.mp4", std::process::id()));
        // Build a synthetic MP4 with ftyp + moov(mvhd + trak(mdia(mdhd + hdlr=vide)))
        let mut bytes = Vec::new();
        // ftyp box (16 bytes)
        bytes.extend_from_slice(&16u32.to_be_bytes());
        bytes.extend_from_slice(b"ftypisom");
        bytes.extend_from_slice(&0u32.to_be_bytes());

        // hdlr box for video ('vide'): 8 + 12 = 20 bytes
        let mut hdlr_vide = Vec::new();
        hdlr_vide.extend_from_slice(&20u32.to_be_bytes());
        hdlr_vide.extend_from_slice(b"hdlr");
        hdlr_vide.extend_from_slice(&[0u8; 8]); // version/flags + pre_defined
        hdlr_vide.extend_from_slice(b"vide");

        // mdhd box (v0): 8 + 20 = 28 bytes (timescale=1000, duration=5400 -> 5.4s)
        let mut mdhd = Vec::new();
        mdhd.extend_from_slice(&28u32.to_be_bytes());
        mdhd.extend_from_slice(b"mdhd");
        mdhd.extend_from_slice(&[0u8; 12]); // v0 + flags + creation + mod
        mdhd.extend_from_slice(&1000u32.to_be_bytes());
        mdhd.extend_from_slice(&5400u32.to_be_bytes());

        // mdia box
        let mdia_len = (8 + mdhd.len() + hdlr_vide.len()) as u32;
        let mut mdia = Vec::new();
        mdia.extend_from_slice(&mdia_len.to_be_bytes());
        mdia.extend_from_slice(b"mdia");
        mdia.extend_from_slice(&mdhd);
        mdia.extend_from_slice(&hdlr_vide);

        // trak box
        let trak_len = (8 + mdia.len()) as u32;
        let mut trak = Vec::new();
        trak.extend_from_slice(&trak_len.to_be_bytes());
        trak.extend_from_slice(b"trak");
        trak.extend_from_slice(&mdia);

        // moov box
        let moov_len = (8 + trak.len()) as u32;
        bytes.extend_from_slice(&moov_len.to_be_bytes());
        bytes.extend_from_slice(b"moov");
        bytes.extend_from_slice(&trak);

        {
            let mut f = File::create(&tmp).unwrap();
            f.write_all(&bytes).unwrap();
        }
        let entry = probe_single_file(tmp.to_str().unwrap());
        let _ = std::fs::remove_file(&tmp);
        assert!(!entry.has_audio, "video-only mp4 must report has_audio=false");
    }
}
