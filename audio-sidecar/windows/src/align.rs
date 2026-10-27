use std::collections::HashMap;

#[derive(Debug, Clone, PartialEq)]
pub struct WordTiming {
    pub word: String,
    pub start: f64,
    pub end: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TimedSpeakerSegment {
    pub speaker_id: String,
    pub start: f64,
    pub end: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TranscriptSegment {
    pub start: f64,
    pub end: f64,
    pub speaker: Option<String>,
    pub text: String,
}

#[derive(Debug, Clone)]
struct AttributedWord {
    word: String,
    start: f64,
    end: f64,
    raw_speaker: String,
}

pub fn round_millis(val: f64) -> f64 {
    if !val.is_finite() {
        0.0
    } else {
        (val * 100.0).round() / 100.0
    }
}

pub fn join_words_cleanly(words: &[String]) -> String {
    let mut result = String::new();
    let no_space_before = [".", ",", "!", "?", ";", ":", "%", ")", "]", "}"];
    for w in words {
        let trimmed = w.trim();
        if trimmed.is_empty() {
            continue;
        }
        if result.is_empty() {
            result.push_str(trimmed);
        } else if no_space_before.contains(&trimmed) {
            result.push_str(trimmed);
        } else {
            result.push(' ');
            result.push_str(trimmed);
        }
    }
    result.trim().to_string()
}

pub fn assign_speaker(
    word_start: f64,
    word_end: f64,
    segments: &[TimedSpeakerSegment],
) -> String {
    if segments.is_empty() {
        return "S1".to_string();
    }

    let mut best_speaker = segments[0].speaker_id.as_str();
    let mut best_overlap = 0.0f64;

    for seg in segments {
        let overlap = word_end.min(seg.end) - word_start.max(seg.start);
        if overlap > best_overlap {
            best_overlap = overlap;
            best_speaker = seg.speaker_id.as_str();
        }
    }

    if best_overlap > 0.0 {
        return best_speaker.to_string();
    }

    // Nearest segment fallback when word falls in a brief inter-segment boundary
    let mid = 0.5 * (word_start + word_end);
    let mut min_dist = f64::INFINITY;
    for seg in segments {
        let dist = if mid < seg.start {
            seg.start - mid
        } else if mid > seg.end {
            mid - seg.end
        } else {
            0.0
        };
        if dist < min_dist {
            min_dist = dist;
            best_speaker = seg.speaker_id.as_str();
        }
    }
    best_speaker.to_string()
}

pub fn build_diarized_segments(
    words: &[WordTiming],
    fallback_text: &str,
    duration_sec: f64,
    speaker_segments: &[TimedSpeakerSegment],
) -> (Vec<TranscriptSegment>, usize) {
    if words.is_empty() {
        let trimmed = fallback_text.trim();
        if trimmed.is_empty() {
            return (Vec::new(), 0);
        }
        return (
            vec![TranscriptSegment {
                start: 0.0,
                end: round_millis(duration_sec.max(0.0)),
                speaker: Some("Speaker 1".to_string()),
                text: trimmed.to_string(),
            }],
            1,
        );
    }

    let mut attributed: Vec<AttributedWord> = words
        .iter()
        .map(|w| {
            let max_t = if duration_sec > 0.0 { duration_sec } else { w.end };
            let s = w.start.max(0.0).min(max_t);
            let e = w.end.max(s).min(max_t);
            AttributedWord {
                word: w.word.clone(),
                start: s,
                end: e,
                raw_speaker: assign_speaker(w.start, w.end, speaker_segments),
            }
        })
        .collect();

    // Smooth isolated 1-word micro-flips (< 0.55s) sandwiched tightly between identical speakers
    if attributed.len() >= 3 {
        for i in 1..(attributed.len() - 1) {
            let prev_spk = attributed[i - 1].raw_speaker.clone();
            let prev_end = attributed[i - 1].end;
            let next_spk = attributed[i + 1].raw_speaker.clone();
            let next_start = attributed[i + 1].start;
            let curr = &attributed[i];
            if prev_spk == next_spk
                && curr.raw_speaker != prev_spk
                && (curr.end - curr.start) < 0.55
                && (curr.start - prev_end) < 0.75
                && (next_start - curr.end) < 0.75
            {
                attributed[i].raw_speaker = prev_spk;
            }
        }
    }

    // Normalize raw cluster IDs ("S2", "S0", ...) into chronological "Speaker 1", "Speaker 2", ...
    let mut speaker_map: HashMap<String, String> = HashMap::new();
    let mut next_speaker_num = 1usize;
    let mut canonical_speaker = |raw: &str| -> String {
        if let Some(existing) = speaker_map.get(raw) {
            existing.clone()
        } else {
            let label = format!("Speaker {}", next_speaker_num);
            next_speaker_num += 1;
            speaker_map.insert(raw.to_string(), label.clone());
            label
        }
    };

    let mut out: Vec<TranscriptSegment> = Vec::new();
    let mut cur_speaker = canonical_speaker(&attributed[0].raw_speaker);
    let mut cur_start = attributed[0].start;
    let mut cur_end = attributed[0].end;
    let mut cur_words: Vec<String> = vec![attributed[0].word.clone()];

    let pause_split_threshold = 3.0;

    for item in attributed.iter().skip(1) {
        let spk = canonical_speaker(&item.raw_speaker);
        let gap = item.start - cur_end;
        if spk != cur_speaker || gap > pause_split_threshold {
            let text = join_words_cleanly(&cur_words);
            if !text.is_empty() {
                out.push(TranscriptSegment {
                    start: round_millis(cur_start),
                    end: round_millis(cur_end),
                    speaker: Some(cur_speaker),
                    text,
                });
            }
            cur_speaker = spk;
            cur_start = item.start;
            cur_end = item.end;
            cur_words = vec![item.word.clone()];
        } else {
            cur_end = cur_end.max(item.end);
            cur_words.push(item.word.clone());
        }
    }

    let tail_text = join_words_cleanly(&cur_words);
    if !tail_text.is_empty() {
        out.push(TranscriptSegment {
            start: round_millis(cur_start),
            end: round_millis(cur_end),
            speaker: Some(cur_speaker),
            text: tail_text,
        });
    }

    (out, speaker_map.len())
}

pub fn build_undiarized_segments(
    words: &[WordTiming],
    fallback_text: &str,
    duration_sec: f64,
) -> Vec<TranscriptSegment> {
    if words.is_empty() {
        let trimmed = fallback_text.trim();
        if trimmed.is_empty() {
            return Vec::new();
        }
        return vec![TranscriptSegment {
            start: 0.0,
            end: round_millis(duration_sec.max(0.0)),
            speaker: None,
            text: trimmed.to_string(),
        }];
    }

    let mut out: Vec<TranscriptSegment> = Vec::new();
    let mut cur_start = words[0].start.max(0.0);
    let mut cur_end = words[0].end.max(cur_start);
    let mut cur_words: Vec<String> = vec![words[0].word.clone()];

    for w in words.iter().skip(1) {
        let gap = w.start - cur_end;
        let prev_word = cur_words.last().map(|s| s.as_str()).unwrap_or("");
        let ends_sentence =
            prev_word.ends_with('.') || prev_word.ends_with('?') || prev_word.ends_with('!');
        let elapsed = cur_end - cur_start;

        if gap > 2.5 || (ends_sentence && elapsed >= 15.0) || elapsed >= 45.0 {
            let text = join_words_cleanly(&cur_words);
            if !text.is_empty() {
                out.push(TranscriptSegment {
                    start: round_millis(cur_start),
                    end: round_millis(cur_end),
                    speaker: None,
                    text,
                });
            }
            cur_start = w.start.max(0.0);
            cur_end = w.end.max(cur_start);
            cur_words = vec![w.word.clone()];
        } else {
            cur_end = cur_end.max(w.end);
            cur_words.push(w.word.clone());
        }
    }

    let tail_text = join_words_cleanly(&cur_words);
    if !tail_text.is_empty() {
        out.push(TranscriptSegment {
            start: round_millis(cur_start),
            end: round_millis(cur_end),
            speaker: None,
            text: tail_text,
        });
    }

    out
}

/// Convert subword token timings (SentencePiece `▁` or BPE leading space) into word timings.
pub fn subword_tokens_to_words(tokens: &[(String, f64, f64)]) -> Vec<WordTiming> {
    let mut words: Vec<WordTiming> = Vec::new();
    let mut cur_word = String::new();
    let mut cur_start = 0.0f64;
    let mut cur_end = 0.0f64;

    for (raw_tok, t_start, t_end) in tokens {
        // Skip special control tokens emitted by Whisper or transducer models
        let trimmed_tok = raw_tok.trim();
        if trimmed_tok.is_empty()
            // Whisper specials: [_BEG_], [_SOT_] end in "_]"; timestamp tokens
            // like [_TT_160] end in a digit then "]". Match the family, not one form.
            || (trimmed_tok.starts_with("[_") && trimmed_tok.ends_with(']'))
            || (trimmed_tok.starts_with("<|") && trimmed_tok.ends_with("|>"))
            || trimmed_tok == "<blk>"
            || trimmed_tok == "<sos/eos>"
            || trimmed_tok == "<s>"
            || trimmed_tok == "</s>"
        {
            continue;
        }

        // SentencePiece uses '▁' (\u{2581}) and Whisper BPE uses leading ' '
        let starts_new_word = raw_tok.starts_with(' ') || raw_tok.starts_with('\u{2581}');
        let clean_piece = raw_tok
            .replace('\u{2581}', " ")
            .trim()
            .to_string();
        if clean_piece.is_empty() {
            continue;
        }

        if starts_new_word || cur_word.is_empty() {
            if !cur_word.is_empty() {
                words.push(WordTiming {
                    word: cur_word.clone(),
                    start: cur_start,
                    end: cur_end.max(cur_start),
                });
            }
            cur_word = clean_piece;
            cur_start = (*t_start).max(0.0);
            cur_end = (*t_end).max(cur_start);
        } else {
            cur_word.push_str(&clean_piece);
            cur_end = cur_end.max(*t_end);
        }
    }

    if !cur_word.is_empty() {
        words.push(WordTiming {
            word: cur_word,
            start: cur_start,
            end: cur_end.max(cur_start),
        });
    }

    words
}

/// Interpolate word timings across a segment `[seg_start, seg_end]` when token-level timings are absent.
pub fn interpolate_segment_words(text: &str, seg_start: f64, seg_end: f64) -> Vec<WordTiming> {
    let parts: Vec<&str> = text.split_whitespace().collect();
    if parts.is_empty() {
        return Vec::new();
    }
    let dur = (seg_end - seg_start).max(0.05);
    let total_chars: usize = parts.iter().map(|w| w.chars().count().max(1)).sum();
    let mut out = Vec::with_capacity(parts.len());
    let mut cursor = seg_start.max(0.0);

    for (idx, &w) in parts.iter().enumerate() {
        let weight = (w.chars().count().max(1) as f64) / (total_chars.max(1) as f64);
        let w_dur = dur * weight;
        let w_end = if idx + 1 == parts.len() {
            seg_end.max(cursor)
        } else {
            cursor + w_dur
        };
        out.push(WordTiming {
            word: w.to_string(),
            start: cursor,
            end: w_end,
        });
        cursor = w_end;
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_diarized_speaker_renumbering_and_microflip_smoothing() {
        let words = vec![
            WordTiming { word: "Hello".to_string(), start: 0.0, end: 0.4 },
            // Isolated 0.2s micro-flip to S9 sandwiched between S4 words
            WordTiming { word: "there".to_string(), start: 0.45, end: 0.65 },
            WordTiming { word: "everyone.".to_string(), start: 0.70, end: 1.2 },
            // Speaker change to S1
            WordTiming { word: "Thanks!".to_string(), start: 1.5, end: 2.1 },
        ];
        let diar = vec![
            TimedSpeakerSegment { speaker_id: "S4".to_string(), start: 0.0, end: 0.42 },
            TimedSpeakerSegment { speaker_id: "S9".to_string(), start: 0.43, end: 0.66 },
            TimedSpeakerSegment { speaker_id: "S4".to_string(), start: 0.67, end: 1.30 },
            TimedSpeakerSegment { speaker_id: "S1".to_string(), start: 1.40, end: 2.20 },
        ];

        let (segs, spk_count) = build_diarized_segments(&words, "", 2.5, &diar);
        assert_eq!(spk_count, 2);
        assert_eq!(segs.len(), 2);
        assert_eq!(segs[0].speaker.as_deref(), Some("Speaker 1"));
        assert_eq!(segs[0].text, "Hello there everyone.");
        assert_eq!(segs[1].speaker.as_deref(), Some("Speaker 2"));
        assert_eq!(segs[1].text, "Thanks!");
    }

    #[test]
    fn test_subword_tokens_to_words() {
        let tokens = vec![
            ("[_BEG_]".to_string(), 0.0, 0.0),
            (" Hel".to_string(), 0.10, 0.25),
            ("lo".to_string(), 0.25, 0.40),
            ("\u{2581}world".to_string(), 0.45, 0.80),
            ("!".to_string(), 0.80, 0.85),
        ];
        let words = subword_tokens_to_words(&tokens);
        assert_eq!(words.len(), 2);
        assert_eq!(words[0].word, "Hello");
        assert!((words[0].start - 0.10).abs() < 1e-6);
        assert!((words[0].end - 0.40).abs() < 1e-6);
        assert_eq!(words[1].word, "world!");
    }
}
