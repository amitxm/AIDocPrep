use std::io::{self, Write};
use std::sync::Mutex;

use crate::align::TranscriptSegment;
use crate::hardware::HardwareInfo;
use crate::probe::ProbeEntry;

static STDOUT_LOCK: Mutex<()> = Mutex::new(());

pub fn emit_json_line(json_line: &str) {
    let _guard = STDOUT_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut out = io::stdout().lock();
    let _ = out.write_all(json_line.as_bytes());
    let _ = out.write_all(b"\n");
    let _ = out.flush();
}

pub fn escape_json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\x08' => out.push_str("\\b"),
            '\x0C' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

pub fn format_f64(val: f64) -> String {
    if !val.is_finite() {
        return "0.0".to_string();
    }
    let rounded = (val * 100.0).round() / 100.0;
    if (rounded - rounded.trunc()).abs() < 1e-9 {
        format!("{:.1}", rounded)
    } else {
        let s = format!("{:.2}", rounded);
        if s.ends_with('0') && !s.ends_with(".0") {
            s.trim_end_matches('0').to_string()
        } else {
            s
        }
    }
}

pub fn emit_hardware(hw: &HardwareInfo) {
    let line = format!(
        "{{\"os\":{},\"arch\":{},\"chip_or_gpu\":{},\"gpu_vendor\":{},\"vram_mb\":{},\"ram_mb\":{},\"vulkan\":{},\"avx2\":{}}}",
        escape_json_str(&hw.os),
        escape_json_str(&hw.arch),
        escape_json_str(&hw.chip_or_gpu),
        escape_json_str(&hw.gpu_vendor),
        hw.vram_mb,
        hw.ram_mb,
        hw.vulkan,
        hw.avx2
    );
    emit_json_line(&line);
}

pub fn emit_probe_entries(entries: &[ProbeEntry]) {
    let mut buf = String::from("[");
    for (idx, e) in entries.iter().enumerate() {
        if idx > 0 {
            buf.push(',');
        }
        buf.push_str(&format!(
            "{{\"path\":{},\"duration_sec\":{},\"has_audio\":{}}}",
            escape_json_str(&e.path),
            format_f64(e.duration_sec),
            e.has_audio
        ));
    }
    buf.push(']');
    emit_json_line(&buf);
}

pub fn emit_progress(stage: &str, pct: i32) {
    let clamped = pct.clamp(0, 100);
    let line = format!(
        "{{\"event\":\"progress\",\"stage\":{},\"pct\":{}}}",
        escape_json_str(stage),
        clamped
    );
    emit_json_line(&line);
}

pub fn emit_download(stage: &str, pct: i32, completed_files: usize, total_files: usize) {
    let clamped = pct.clamp(0, 100);
    let line = format!(
        "{{\"event\":\"download\",\"stage\":{},\"pct\":{},\"completed_files\":{},\"total_files\":{}}}",
        escape_json_str(stage),
        clamped,
        completed_files,
        total_files
    );
    emit_json_line(&line);
}

pub fn emit_result(duration_sec: f64, speakers: usize, segments: &[TranscriptSegment]) {
    let mut seg_buf = String::from("[");
    for (idx, seg) in segments.iter().enumerate() {
        if idx > 0 {
            seg_buf.push(',');
        }
        let spk_json = match &seg.speaker {
            Some(s) => escape_json_str(s),
            None => "null".to_string(),
        };
        seg_buf.push_str(&format!(
            "{{\"start\":{},\"end\":{},\"speaker\":{},\"text\":{}}}",
            format_f64(seg.start),
            format_f64(seg.end),
            spk_json,
            escape_json_str(&seg.text)
        ));
    }
    seg_buf.push(']');

    let line = format!(
        "{{\"event\":\"result\",\"duration_sec\":{},\"speakers\":{},\"segments\":{}}}",
        format_f64(duration_sec),
        speakers,
        seg_buf
    );
    emit_json_line(&line);
}

pub fn emit_error(code: &str, message: &str) {
    let line = format!(
        "{{\"event\":\"error\",\"code\":{},\"message\":{}}}",
        escape_json_str(code),
        escape_json_str(message)
    );
    emit_json_line(&line);
}

// MARK: - Lightweight RFC-8259 JSON Parser (for whisper-cli & ffprobe JSON outputs)

#[derive(Debug, Clone, PartialEq)]
pub enum JsonValue {
    Null,
    Bool(bool),
    Number(f64),
    Str(String),
    Array(Vec<JsonValue>),
    Object(Vec<(String, JsonValue)>),
}

impl JsonValue {
    pub fn get(&self, key: &str) -> Option<&JsonValue> {
        if let JsonValue::Object(entries) = self {
            for (k, v) in entries {
                if k == key {
                    return Some(v);
                }
            }
        }
        None
    }

    pub fn as_str(&self) -> Option<&str> {
        if let JsonValue::Str(s) = self {
            Some(s.as_str())
        } else {
            None
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            JsonValue::Number(n) => Some(*n),
            JsonValue::Str(s) => s.trim().parse::<f64>().ok(),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[JsonValue]> {
        if let JsonValue::Array(arr) = self {
            Some(arr.as_slice())
        } else {
            None
        }
    }
}

pub fn parse_json(input: &str) -> Result<JsonValue, String> {
    let bytes = input.as_bytes();
    let mut pos = 0;
    // Skip UTF-8 BOM if present
    if bytes.starts_with(&[0xEF, 0xBB, 0xBF]) {
        pos = 3;
    }
    skip_ws(bytes, &mut pos);
    let val = parse_value(bytes, &mut pos)?;
    skip_ws(bytes, &mut pos);
    if pos != bytes.len() {
        return Err(format!("Unexpected trailing data at byte {}", pos));
    }
    Ok(val)
}

fn skip_ws(bytes: &[u8], pos: &mut usize) {
    while *pos < bytes.len() {
        match bytes[*pos] {
            b' ' | b'\t' | b'\r' | b'\n' => *pos += 1,
            _ => break,
        }
    }
}

fn parse_value(bytes: &[u8], pos: &mut usize) -> Result<JsonValue, String> {
    skip_ws(bytes, pos);
    if *pos >= bytes.len() {
        return Err("Unexpected end of JSON input".to_string());
    }
    match bytes[*pos] {
        b'"' => parse_string(bytes, pos).map(JsonValue::Str),
        b'{' => parse_object(bytes, pos),
        b'[' => parse_array(bytes, pos),
        b't' => {
            if bytes[*pos..].starts_with(b"true") {
                *pos += 4;
                Ok(JsonValue::Bool(true))
            } else {
                Err(format!("Invalid literal at {}", *pos))
            }
        }
        b'f' => {
            if bytes[*pos..].starts_with(b"false") {
                *pos += 5;
                Ok(JsonValue::Bool(false))
            } else {
                Err(format!("Invalid literal at {}", *pos))
            }
        }
        b'n' => {
            if bytes[*pos..].starts_with(b"null") {
                *pos += 4;
                Ok(JsonValue::Null)
            } else {
                Err(format!("Invalid literal at {}", *pos))
            }
        }
        b'-' | b'0'..=b'9' => parse_number(bytes, pos),
        other => Err(format!("Unexpected byte 0x{:02x} at {}", other, *pos)),
    }
}

fn parse_string(bytes: &[u8], pos: &mut usize) -> Result<String, String> {
    if *pos >= bytes.len() || bytes[*pos] != b'"' {
        return Err("Expected '\"'".to_string());
    }
    *pos += 1;
    let mut out = String::new();
    while *pos < bytes.len() {
        let b = bytes[*pos];
        if b == b'"' {
            *pos += 1;
            return Ok(out);
        }
        if b == b'\\' {
            *pos += 1;
            if *pos >= bytes.len() {
                return Err("Unterminated escape sequence".to_string());
            }
            let esc = bytes[*pos];
            *pos += 1;
            match esc {
                b'"' => out.push('"'),
                b'\\' => out.push('\\'),
                b'/' => out.push('/'),
                b'b' => out.push('\x08'),
                b'f' => out.push('\x0C'),
                b'n' => out.push('\n'),
                b'r' => out.push('\r'),
                b't' => out.push('\t'),
                b'u' => {
                    let cp1 = parse_hex4(bytes, pos)?;
                    if (0xD800..=0xDBFF).contains(&cp1) {
                        // UTF-16 high surrogate: check for low surrogate \uDC00..\uDFFF
                        if *pos + 6 <= bytes.len() && bytes[*pos] == b'\\' && bytes[*pos + 1] == b'u' {
                            *pos += 2;
                            let cp2 = parse_hex4(bytes, pos)?;
                            if (0xDC00..=0xDFFF).contains(&cp2) {
                                let combined = 0x10000 + (((cp1 as u32 - 0xD800) << 10) | (cp2 as u32 - 0xDC00));
                                if let Some(ch) = char::from_u32(combined) {
                                    out.push(ch);
                                    continue;
                                }
                            }
                        }
                        out.push('\u{FFFD}');
                    } else if let Some(ch) = char::from_u32(cp1 as u32) {
                        out.push(ch);
                    } else {
                        out.push('\u{FFFD}');
                    }
                }
                _ => out.push(esc as char),
            }
        } else {
            // Decode next UTF-8 codepoint from slice
            let remaining = &bytes[*pos..];
            let s = std::str::from_utf8(remaining).map_err(|e| {
                let valid = e.valid_up_to();
                if valid > 0 {
                    String::new()
                } else {
                    format!("Invalid UTF-8 in JSON string at {}", *pos)
                }
            });
            match s {
                Ok(valid_str) => {
                    if let Some(ch) = valid_str.chars().next() {
                        out.push(ch);
                        *pos += ch.len_utf8();
                    } else {
                        break;
                    }
                }
                Err(_) => {
                    // Partial valid prefix or replacement
                    if let Err(utf_err) = std::str::from_utf8(remaining) {
                        let valid_len = utf_err.valid_up_to();
                        if valid_len > 0 {
                            let prefix = std::str::from_utf8(&remaining[..valid_len]).unwrap();
                            let ch = prefix.chars().next().unwrap();
                            out.push(ch);
                            *pos += ch.len_utf8();
                        } else {
                            out.push('\u{FFFD}');
                            *pos += 1;
                        }
                    }
                }
            }
        }
    }
    Err("Unterminated JSON string".to_string())
}

fn parse_hex4(bytes: &[u8], pos: &mut usize) -> Result<u16, String> {
    if *pos + 4 > bytes.len() {
        return Err("Short \\uXXXX escape".to_string());
    }
    let hex_str = std::str::from_utf8(&bytes[*pos..*pos + 4])
        .map_err(|_| "Invalid hex digits".to_string())?;
    let val = u16::from_str_radix(hex_str, 16).map_err(|_| "Invalid hex escape".to_string())?;
    *pos += 4;
    Ok(val)
}

fn parse_number(bytes: &[u8], pos: &mut usize) -> Result<JsonValue, String> {
    let start = *pos;
    if bytes[*pos] == b'-' {
        *pos += 1;
    }
    while *pos < bytes.len() && bytes[*pos].is_ascii_digit() {
        *pos += 1;
    }
    if *pos < bytes.len() && bytes[*pos] == b'.' {
        *pos += 1;
        while *pos < bytes.len() && bytes[*pos].is_ascii_digit() {
            *pos += 1;
        }
    }
    if *pos < bytes.len() && (bytes[*pos] == b'e' || bytes[*pos] == b'E') {
        *pos += 1;
        if *pos < bytes.len() && (bytes[*pos] == b'+' || bytes[*pos] == b'-') {
            *pos += 1;
        }
        while *pos < bytes.len() && bytes[*pos].is_ascii_digit() {
            *pos += 1;
        }
    }
    let num_str = std::str::from_utf8(&bytes[start..*pos])
        .map_err(|_| "Invalid number UTF-8".to_string())?;
    let val = num_str
        .parse::<f64>()
        .map_err(|_| format!("Invalid number {:?}", num_str))?;
    Ok(JsonValue::Number(val))
}

fn parse_array(bytes: &[u8], pos: &mut usize) -> Result<JsonValue, String> {
    *pos += 1; // skip '['
    skip_ws(bytes, pos);
    let mut items = Vec::new();
    if *pos < bytes.len() && bytes[*pos] == b']' {
        *pos += 1;
        return Ok(JsonValue::Array(items));
    }
    loop {
        let item = parse_value(bytes, pos)?;
        items.push(item);
        skip_ws(bytes, pos);
        if *pos >= bytes.len() {
            return Err("Unterminated JSON array".to_string());
        }
        match bytes[*pos] {
            b',' => {
                *pos += 1;
                skip_ws(bytes, pos);
            }
            b']' => {
                *pos += 1;
                return Ok(JsonValue::Array(items));
            }
            other => return Err(format!("Expected ',' or ']' in array, got 0x{:02x}", other)),
        }
    }
}

fn parse_object(bytes: &[u8], pos: &mut usize) -> Result<JsonValue, String> {
    *pos += 1; // skip '{'
    skip_ws(bytes, pos);
    let mut entries = Vec::new();
    if *pos < bytes.len() && bytes[*pos] == b'}' {
        *pos += 1;
        return Ok(JsonValue::Object(entries));
    }
    loop {
        skip_ws(bytes, pos);
        let key = parse_string(bytes, pos)?;
        skip_ws(bytes, pos);
        if *pos >= bytes.len() || bytes[*pos] != b':' {
            return Err("Expected ':' after object key".to_string());
        }
        *pos += 1;
        let val = parse_value(bytes, pos)?;
        entries.push((key, val));
        skip_ws(bytes, pos);
        if *pos >= bytes.len() {
            return Err("Unterminated JSON object".to_string());
        }
        match bytes[*pos] {
            b',' => {
                *pos += 1;
                skip_ws(bytes, pos);
            }
            b'}' => {
                *pos += 1;
                return Ok(JsonValue::Object(entries));
            }
            other => return Err(format!("Expected ',' or '}}' in object, got 0x{:02x}", other)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_escape_and_parse_roundtrip() {
        let raw = r#"{"transcription":[{"offsets":{"from":0,"to":1420},"text":" Hello \"world\"\nNext","tokens":[{"text":" Hello","offsets":{"from":0,"to":480}}]}]}"#;
        let parsed = parse_json(raw).expect("valid json");
        let arr = parsed.get("transcription").and_then(|v| v.as_array()).unwrap();
        assert_eq!(arr.len(), 1);
        let first = &arr[0];
        assert_eq!(first.get("text").and_then(|v| v.as_str()), Some(" Hello \"world\"\nNext"));
        let from_ms = first
            .get("offsets")
            .and_then(|o| o.get("from"))
            .and_then(|n| n.as_f64());
        assert_eq!(from_ms, Some(0.0));
    }
}
