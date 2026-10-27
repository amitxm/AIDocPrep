import AVFoundation
import AudioToolbox
import CoreMedia
import Darwin
import FluidAudio
import Foundation
import os

// MARK: - JSON Output Models & Thread-Safe Line Emitter

private let stdoutLock = OSAllocatedUnfairLock()

private func emitJSON<T: Encodable>(_ value: T) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.withoutEscapingSlashes]
    guard let data = try? encoder.encode(value) else { return }
    stdoutLock.withLock {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }
}

private struct HardwareInfo: Encodable {
    let os: String
    let arch: String
    let chip_or_gpu: String
    let gpu_vendor: String
    let vram_mb: Int
    let ram_mb: Int
    let vulkan: Bool
    let avx2: Bool
}

private struct ProbeEntry: Encodable {
    let path: String
    let duration_sec: Double
    let has_audio: Bool
}

private struct ProgressEvent: Encodable {
    var event: String = "progress"
    let stage: String
    let pct: Int
}

private struct DownloadEvent: Encodable {
    var event: String = "download"
    let stage: String
    let pct: Int
    let completed_files: Int
    let total_files: Int
}

private struct TranscriptSegment: Encodable {
    let start: Double
    let end: Double
    let speaker: String?
    let text: String
}

private struct TranscribeResultEvent: Encodable {
    var event: String = "result"
    let duration_sec: Double
    let speakers: Int
    let segments: [TranscriptSegment]
}

private struct ErrorEvent: Encodable {
    var event: String = "error"
    let code: String
    let message: String
}

private enum SidecarError: Error, LocalizedError {
    case invalidArgs(String)
    case fileNotFound(String)
    case modelMissing(String)
    case noAudioTrack(String)
    case decodeFailed(String)
    case inferenceFailed(String)

    var code: String {
        switch self {
        case .invalidArgs: return "invalid_args"
        case .fileNotFound: return "file_not_found"
        case .modelMissing: return "model_missing"
        case .noAudioTrack: return "no_audio"
        case .decodeFailed: return "decode_error"
        case .inferenceFailed: return "inference_error"
        }
    }

    var exitCode: Int32 {
        switch self {
        case .invalidArgs, .fileNotFound, .modelMissing: return 2
        case .noAudioTrack: return 3
        case .decodeFailed, .inferenceFailed: return 1
        }
    }

    var errorDescription: String? {
        switch self {
        case .invalidArgs(let m),
            .fileNotFound(let m),
            .modelMissing(let m),
            .noAudioTrack(let m),
            .decodeFailed(let m),
            .inferenceFailed(let m):
            return m
        }
    }
}

// MARK: - Hardware Detection

private func sysctlString(_ name: String) -> String? {
    var size: Int = 0
    guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else { return nil }
    var buffer = [CChar](repeating: 0, count: size)
    guard sysctlbyname(name, &buffer, &size, nil, 0) == 0 else { return nil }
    let bytes = buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }
    return String(bytes: bytes, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
}

private func detectHardware() -> HardwareInfo {
    let brand = sysctlString("machdep.cpu.brand_string") ?? "Apple Silicon"
    let memBytes = ProcessInfo.processInfo.physicalMemory
    let memMB = Int(memBytes / (1024 * 1024))
    #if arch(arm64)
    let arch = "aarch64"
    let vendor = "apple"
    #elseif arch(x86_64)
    let arch = "x86_64"
    let vendor = "intel"
    #else
    let arch = "unknown"
    let vendor = "unknown"
    #endif
    return HardwareInfo(
        os: "macos",
        arch: arch,
        chip_or_gpu: brand,
        gpu_vendor: vendor,
        vram_mb: memMB,
        ram_mb: memMB,
        vulkan: false,
        avx2: false
    )
}

// MARK: - Container Probing (AVFoundation + Ogg + EBML/Matroska + ffprobe fallback)

private func probeOgg(url: URL) -> (duration: Double, hasAudio: Bool)? {
    guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
    defer { try? handle.close() }

    guard let head = try? handle.read(upToCount: 8192), head.count >= 36 else { return nil }
    guard head[0] == 0x4F, head[1] == 0x67, head[2] == 0x67, head[3] == 0x53 else { return nil }

    var sampleRate: Double = 0
    var preSkip: Int64 = 0
    if let opusRange = head.range(of: Data("OpusHead".utf8)), opusRange.upperBound + 4 <= head.count {
        sampleRate = 48000.0  // Ogg Opus granule positions are always 48 kHz
        let base = opusRange.lowerBound
        if base + 12 <= head.count {
            let lo = UInt16(head[base + 10])
            let hi = UInt16(head[base + 11]) << 8
            preSkip = Int64(lo | hi)
        }
    } else if let vorbisRange = head.range(of: Data([0x01, 0x76, 0x6F, 0x72, 0x62, 0x69, 0x73])),
        vorbisRange.lowerBound + 16 <= head.count
    {
        let base = vorbisRange.lowerBound
        let sr =
            UInt32(head[base + 12])
            | (UInt32(head[base + 13]) << 8)
            | (UInt32(head[base + 14]) << 16)
            | (UInt32(head[base + 15]) << 24)
        if sr > 0 { sampleRate = Double(sr) }
    }

    guard sampleRate > 0 else { return (0, true) }

    let fileSize = (try? handle.seekToEnd()) ?? UInt64(head.count)
    let tailRead: UInt64 = min(fileSize, 65536)
    try? handle.seek(toOffset: fileSize - tailRead)
    guard let tail = try? handle.readToEnd(), tail.count >= 27 else { return (0, true) }

    var lastGranule: Int64 = 0
    let magic = Data("OggS".utf8)
    var searchRange = tail.startIndex..<tail.endIndex
    while let match = tail.range(of: magic, options: [], in: searchRange) {
        let pos = match.lowerBound
        if pos + 14 <= tail.endIndex {
            var g: UInt64 = 0
            for b in 0..<8 {
                g |= UInt64(tail[pos + 6 + b]) << (b * 8)
            }
            let signed = Int64(bitPattern: g)
            if signed > lastGranule {
                lastGranule = signed
            }
        }
        searchRange = (pos + 4)..<tail.endIndex
    }

    let dur = max(0.0, Double(max(0, lastGranule - preSkip)) / sampleRate)
    return (dur, true)
}

private func readEBMLVint(_ data: Data, at index: inout Int, maskLength: Bool) -> UInt64? {
    guard index < data.count else { return nil }
    let first = data[index]
    guard first != 0 else { return nil }
    var length = 1
    var mask: UInt8 = 0x80
    while (first & mask) == 0 {
        length += 1
        mask >>= 1
        if length > 8 { return nil }
    }
    guard index + length <= data.count else { return nil }
    var value = UInt64(maskLength ? (first & (mask - 1)) : first)
    for i in 1..<length {
        value = (value << 8) | UInt64(data[index + i])
    }
    index += length
    return value
}

private func probeMatroska(url: URL) -> (duration: Double, hasAudio: Bool)? {
    guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
    defer { try? handle.close() }
    guard let data = try? handle.read(upToCount: 262_144), data.count >= 4 else { return nil }
    guard data[0] == 0x1A, data[1] == 0x45, data[2] == 0xDF, data[3] == 0xA3 else { return nil }

    var idx = 0
    var timestampScale: Double = 1_000_000.0  // default 1ms in ns
    var rawDuration: Double?
    var hasAudio = false

    // Master elements we descend into: EBML, Segment, Info, Tracks, TrackEntry
    let masterIDs: Set<UInt64> = [
        0x1A45_DFA3, 0x1853_8067, 0x1549_A966, 0x1654_AE6B, 0xAE,
    ]

    while idx < data.count {
        let startIdx = idx
        guard let elemID = readEBMLVint(data, at: &idx, maskLength: false),
            let elemSize = readEBMLVint(data, at: &idx, maskLength: true)
        else {
            idx = startIdx + 1
            continue
        }
        // Stop once we hit Cluster (0x1F43B675) — metadata headers precede clusters
        if elemID == 0x1F43_B675 { break }

        if masterIDs.contains(elemID) {
            continue
        }

        let size = Int(min(elemSize, UInt64(data.count - idx)))
        if elemID == 0x2AD7_B1, size >= 1 && size <= 8 {
            // TimestampScale (uint)
            var v: UInt64 = 0
            for i in 0..<size { v = (v << 8) | UInt64(data[idx + i]) }
            if v > 0 { timestampScale = Double(v) }
        } else if elemID == 0x4489, size == 4 || size == 8 {
            // Duration (float)
            if size == 4 {
                var bits: UInt32 = 0
                for i in 0..<4 { bits = (bits << 8) | UInt32(data[idx + i]) }
                rawDuration = Double(Float(bitPattern: bits))
            } else {
                var bits: UInt64 = 0
                for i in 0..<8 { bits = (bits << 8) | UInt64(data[idx + i]) }
                rawDuration = Double(bitPattern: bits)
            }
        } else if elemID == 0x83, size == 1 {
            // TrackType: 2 == audio
            if data[idx] == 2 { hasAudio = true }
        }

        if elemSize >= 0x00FF_FFFF_FFFF_FFFF {
            break
        }
        idx += size
    }

    let durationSec = (rawDuration ?? 0.0) * timestampScale / 1_000_000_000.0
    return (max(0.0, durationSec), hasAudio)
}

private func findExecutable(_ name: String) -> String? {
    let candidates = [
        "/opt/homebrew/bin/\(name)",
        "/usr/local/bin/\(name)",
        "/usr/bin/\(name)",
    ]
    for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
        return c
    }
    if let pathEnv = ProcessInfo.processInfo.environment["PATH"] {
        for dir in pathEnv.split(separator: ":") {
            let full = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: full) {
                return full
            }
        }
    }
    return nil
}

private func probeWithFFprobe(url: URL) -> (duration: Double, hasAudio: Bool)? {
    guard let ffprobe = findExecutable("ffprobe") else { return nil }
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: ffprobe)
    proc.arguments = [
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type:format=duration",
        "-of", "json",
        url.path,
    ]
    let outPipe = Pipe()
    proc.standardOutput = outPipe
    proc.standardError = FileHandle.nullDevice
    do {
        try proc.run()
        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard proc.terminationStatus == 0,
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        let streams = obj["streams"] as? [[String: Any]] ?? []
        let hasAudio = !streams.isEmpty
        var dur = 0.0
        if let fmt = obj["format"] as? [String: Any],
            let durStr = fmt["duration"] as? String,
            let d = Double(durStr), d.isFinite
        {
            dur = max(0.0, d)
        }
        return (dur, hasAudio)
    } catch {
        return nil
    }
}

private let pureAudioExtensions: Set<String> = ["wav", "mp3", "m4a", "flac", "aac"]

private func probeSingleFile(_ path: String) async -> ProbeEntry {
    let url = URL(fileURLWithPath: path)
    guard FileManager.default.fileExists(atPath: url.path) else {
        return ProbeEntry(path: path, duration_sec: 0, has_audio: false)
    }

    let ext = url.pathExtension.lowercased()

    // 1. Fast path for standard CoreAudio files
    if pureAudioExtensions.contains(ext) {
        if let audioFile = try? AVAudioFile(forReading: url) {
            let sr = audioFile.processingFormat.sampleRate
            let dur = sr > 0 ? Double(audioFile.length) / sr : 0.0
            return ProbeEntry(
                path: path,
                duration_sec: (dur * 10).rounded() / 10.0,
                has_audio: audioFile.length > 0
            )
        }
    }

    // 2. Ogg / Opus header + granule probe
    if let ogg = probeOgg(url: url) {
        return ProbeEntry(
            path: path,
            duration_sec: (ogg.duration * 10).rounded() / 10.0,
            has_audio: ogg.hasAudio
        )
    }

    // 3. Matroska / WebM EBML header probe
    if let mkv = probeMatroska(url: url) {
        if mkv.duration > 0 || !mkv.hasAudio {
            return ProbeEntry(
                path: path,
                duration_sec: (mkv.duration * 10).rounded() / 10.0,
                has_audio: mkv.hasAudio
            )
        }
        if let ffp = probeWithFFprobe(url: url) {
            return ProbeEntry(
                path: path,
                duration_sec: (ffp.duration * 10).rounded() / 10.0,
                has_audio: ffp.hasAudio
            )
        }
        return ProbeEntry(path: path, duration_sec: 0, has_audio: mkv.hasAudio)
    }

    // 4. AVURLAsset for MP4 / MOV / QuickTime / general AVFoundation containers
    let asset = AVURLAsset(url: url)
    if let tracks = try? await asset.loadTracks(withMediaType: .audio),
        let cmDur = try? await asset.load(.duration)
    {
        let secs = CMTimeGetSeconds(cmDur)
        let dur = secs.isFinite && secs > 0 ? secs : 0.0
        if !tracks.isEmpty || dur > 0 {
            return ProbeEntry(
                path: path,
                duration_sec: (dur * 10).rounded() / 10.0,
                has_audio: !tracks.isEmpty
            )
        }
    }

    // 5. Opportunistic ffprobe fallback
    if let ffp = probeWithFFprobe(url: url) {
        return ProbeEntry(
            path: path,
            duration_sec: (ffp.duration * 10).rounded() / 10.0,
            has_audio: ffp.hasAudio
        )
    }

    return ProbeEntry(path: path, duration_sec: 0, has_audio: false)
}

// MARK: - Streaming 16 kHz Mono WAV Writer & Multi-Container Decoder

private final class WavStreamWriter {
    let url: URL
    private let handle: FileHandle
    private(set) var dataBytesWritten: UInt32 = 0
    let sampleRate: UInt32

    init(url: URL, sampleRate: UInt32 = 16000) throws {
        self.url = url
        self.sampleRate = sampleRate
        FileManager.default.createFile(atPath: url.path, contents: nil)
        self.handle = try FileHandle(forWritingTo: url)
        try writeHeader(pcmByteCount: 0)
    }

    private func writeHeader(pcmByteCount: UInt32) throws {
        try handle.seek(toOffset: 0)
        var header = Data(capacity: 44)
        header.append(contentsOf: "RIFF".utf8)
        var riffSize = (36 &+ pcmByteCount).littleEndian
        withUnsafeBytes(of: &riffSize) { header.append(contentsOf: $0) }
        header.append(contentsOf: "WAVEfmt ".utf8)
        var fmtSize = UInt32(16).littleEndian
        withUnsafeBytes(of: &fmtSize) { header.append(contentsOf: $0) }
        var audioFmt = UInt16(1).littleEndian  // PCM Int16
        withUnsafeBytes(of: &audioFmt) { header.append(contentsOf: $0) }
        var channels = UInt16(1).littleEndian
        withUnsafeBytes(of: &channels) { header.append(contentsOf: $0) }
        var sr = sampleRate.littleEndian
        withUnsafeBytes(of: &sr) { header.append(contentsOf: $0) }
        var byteRate = (sampleRate * 2).littleEndian
        withUnsafeBytes(of: &byteRate) { header.append(contentsOf: $0) }
        var blockAlign = UInt16(2).littleEndian
        withUnsafeBytes(of: &blockAlign) { header.append(contentsOf: $0) }
        var bitsPerSample = UInt16(16).littleEndian
        withUnsafeBytes(of: &bitsPerSample) { header.append(contentsOf: $0) }
        header.append(contentsOf: "data".utf8)
        var dataSize = pcmByteCount.littleEndian
        withUnsafeBytes(of: &dataSize) { header.append(contentsOf: $0) }
        try handle.write(contentsOf: header)
    }

    func appendPCM16Bytes(_ data: Data) throws {
        guard !data.isEmpty else { return }
        try handle.write(contentsOf: data)
        dataBytesWritten = dataBytesWritten &+ UInt32(clamping: data.count)
    }

    func appendFloatSamples(_ samples: UnsafeBufferPointer<Float>) throws {
        guard !samples.isEmpty else { return }
        var pcm = Data(count: samples.count * 2)
        pcm.withUnsafeMutableBytes { rawBuf in
            let dst = rawBuf.bindMemory(to: Int16.self)
            for i in 0..<samples.count {
                let s = samples[i]
                let clamped = s.isFinite ? max(-1.0, min(1.0, s)) : 0.0
                dst[i] = Int16(clamped * 32767.0).littleEndian
            }
        }
        try appendPCM16Bytes(pcm)
    }

    var sampleCount: Int {
        Int(dataBytesWritten) / 2
    }

    func padToMinimumSamples(_ minSamples: Int) throws {
        let current = sampleCount
        if current > 0 && current < minSamples {
            let needed = (minSamples - current) * 2
            try appendPCM16Bytes(Data(repeating: 0, count: needed))
        }
    }

    func finish() throws {
        try writeHeader(pcmByteCount: dataBytesWritten)
        try handle.synchronize()
        try handle.close()
    }
}

/// Pure-Swift Ogg Opus demuxer + AudioToolbox Opus decoder -> 16 kHz mono WAV.
private func decodeOggOpusToWav(inputURL: URL, outputWavURL: URL) throws -> Double {
    let fileData = try Data(contentsOf: inputURL, options: [.mappedIfSafe])
    guard fileData.count >= 28,
        fileData[0] == 0x4F, fileData[1] == 0x67, fileData[2] == 0x67, fileData[3] == 0x53
    else {
        throw SidecarError.decodeFailed("Not a valid Ogg container")
    }

    // Reconstruct logical packets across OggS pages
    var packets: [Data] = []
    var currentPacket = Data()
    var pos = 0
    while pos + 27 <= fileData.count {
        guard fileData[pos] == 0x4F, fileData[pos + 1] == 0x67,
            fileData[pos + 2] == 0x67, fileData[pos + 3] == 0x53
        else {
            break
        }
        let numSegments = Int(fileData[pos + 26])
        let segTableStart = pos + 27
        guard segTableStart + numSegments <= fileData.count else { break }
        var payloadPos = segTableStart + numSegments
        for s in 0..<numSegments {
            let segLen = Int(fileData[segTableStart + s])
            guard payloadPos + segLen <= fileData.count else { break }
            currentPacket.append(fileData[payloadPos..<(payloadPos + segLen)])
            payloadPos += segLen
            if segLen < 255 {
                packets.append(currentPacket)
                currentPacket = Data()
            }
        }
        pos = payloadPos
    }
    if !currentPacket.isEmpty {
        packets.append(currentPacket)
    }

    guard packets.count >= 2,
        packets[0].count >= 19,
        String(data: packets[0].prefix(8), encoding: .utf8) == "OpusHead"
    else {
        throw SidecarError.decodeFailed("Unsupported Ogg stream (not Opus)")
    }

    let head = packets[0]
    let channelCount = max(1, min(2, Int(head[9])))
    let preSkip48k = Int(UInt16(head[10]) | (UInt16(head[11]) << 8))

    var inDesc = AudioStreamBasicDescription(
        mSampleRate: 48000,
        mFormatID: kAudioFormatOpus,
        mFormatFlags: 0,
        mBytesPerPacket: 0,
        mFramesPerPacket: 960,
        mBytesPerFrame: 0,
        mChannelsPerFrame: UInt32(channelCount),
        mBitsPerChannel: 0,
        mReserved: 0
    )
    guard let opusFormat = AVAudioFormat(streamDescription: &inDesc),
        let pcm48Format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: 48000,
            channels: AVAudioChannelCount(channelCount),
            interleaved: false
        ),
        let opusDecoder = AVAudioConverter(from: opusFormat, to: pcm48Format)
    else {
        throw SidecarError.decodeFailed("Failed to initialize macOS AudioToolbox Opus decoder")
    }

    var decoded48kMono: [Float] = []
    let maxPacketSize = packets.dropFirst(2).map(\.count).max() ?? 4096

    for pkt in packets.dropFirst(2) where !pkt.isEmpty {
        let compressed = AVAudioCompressedBuffer(
            format: opusFormat,
            packetCapacity: 1,
            maximumPacketSize: max(maxPacketSize, pkt.count)
        )
        pkt.withUnsafeBytes { raw in
            if let base = raw.baseAddress {
                memcpy(compressed.data, base, pkt.count)
            }
        }
        compressed.byteLength = UInt32(pkt.count)
        compressed.packetCount = 1
        compressed.packetDescriptions?.pointee = AudioStreamPacketDescription(
            mStartOffset: 0,
            mVariableFramesInPacket: 0,
            mDataByteSize: UInt32(pkt.count)
        )

        guard let outBuf = AVAudioPCMBuffer(pcmFormat: pcm48Format, frameCapacity: 5760) else {
            continue
        }
        let fed = OSAllocatedUnfairLock(initialState: false)
        nonisolated(unsafe) let capturedCompressed = compressed
        var convErr: NSError?
        let status = opusDecoder.convert(to: outBuf, error: &convErr) { _, outStatus in
            let alreadyFed = fed.withLock { state -> Bool in
                if state { return true }
                state = true
                return false
            }
            if !alreadyFed {
                outStatus.pointee = .haveData
                return capturedCompressed
            }
            outStatus.pointee = .noDataNow
            return nil
        }
        if status == .error || outBuf.frameLength == 0 { continue }
        guard let chData = outBuf.floatChannelData else { continue }
        let frames = Int(outBuf.frameLength)
        if channelCount == 1 {
            decoded48kMono.append(contentsOf: UnsafeBufferPointer(start: chData[0], count: frames))
        } else {
            for f in 0..<frames {
                decoded48kMono.append(0.5 * (chData[0][f] + chData[1][f]))
            }
        }
    }

    if preSkip48k > 0 && preSkip48k < decoded48kMono.count {
        decoded48kMono.removeFirst(preSkip48k)
    }

    let realDuration = Double(decoded48kMono.count) / 48000.0
    let resampled16k = try AudioConverter(sampleRate: 16000).resample(decoded48kMono, from: 48000)
    let writer = try WavStreamWriter(url: outputWavURL, sampleRate: 16000)
    try resampled16k.withUnsafeBufferPointer { ptr in
        try writer.appendFloatSamples(ptr)
    }
    try writer.padToMinimumSamples(16000)
    try writer.finish()
    return realDuration
}

private func decodeViaAVAssetReader(inputURL: URL, outputWavURL: URL) async throws -> Double {
    let asset = AVURLAsset(url: inputURL)
    let audioTracks = try await asset.loadTracks(withMediaType: .audio)
    if audioTracks.isEmpty {
        let videoTracks = (try? await asset.loadTracks(withMediaType: .video)) ?? []
        if !videoTracks.isEmpty {
            throw SidecarError.noAudioTrack("Video file contains no audio track")
        }
        throw SidecarError.decodeFailed("Unsupported container or missing audio stream")
    }

    let reader = try AVAssetReader(asset: asset)
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVSampleRateKey: 16000,
        AVNumberOfChannelsKey: 1,
        AVLinearPCMBitDepthKey: 16,
        AVLinearPCMIsFloatKey: false,
        AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: false,
    ]
    let output = AVAssetReaderTrackOutput(track: audioTracks[0], outputSettings: settings)
    output.alwaysCopiesSampleData = false
    guard reader.canAdd(output) else {
        throw SidecarError.decodeFailed("Cannot attach audio track reader")
    }
    reader.add(output)
    guard reader.startReading() else {
        throw SidecarError.decodeFailed(
            reader.error?.localizedDescription ?? "AVAssetReader failed to start"
        )
    }

    let writer = try WavStreamWriter(url: outputWavURL, sampleRate: 16000)
    while let sampleBuffer = output.copyNextSampleBuffer() {
        if let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) {
            let length = CMBlockBufferGetDataLength(blockBuffer)
            if length > 0 {
                var chunk = Data(count: length)
                let copyStatus = chunk.withUnsafeMutableBytes { rawBuf -> OSStatus in
                    guard let base = rawBuf.baseAddress else { return -1 }
                    return CMBlockBufferCopyDataBytes(blockBuffer, atOffset: 0, dataLength: length, destination: base)
                }
                if copyStatus == kCMBlockBufferNoErr {
                    try writer.appendPCM16Bytes(chunk)
                }
            }
        }
    }

    if reader.status == .failed {
        throw SidecarError.decodeFailed(
            reader.error?.localizedDescription ?? "Audio track decoding failed"
        )
    }

    let realDuration = Double(writer.sampleCount) / 16000.0
    try writer.padToMinimumSamples(16000)
    try writer.finish()
    return realDuration
}

private func decodeViaAVAudioFile(inputURL: URL, outputWavURL: URL) throws -> Double {
    let audioFile = try AVAudioFile(forReading: inputURL)
    let converter = AudioConverter(sampleRate: 16000)
    let samples = try converter.resampleAudioFile(inputURL)
    let realDuration = Double(samples.count) / 16000.0
    let writer = try WavStreamWriter(url: outputWavURL, sampleRate: 16000)
    try samples.withUnsafeBufferPointer { ptr in
        try writer.appendFloatSamples(ptr)
    }
    try writer.padToMinimumSamples(16000)
    try writer.finish()
    _ = audioFile
    return realDuration
}

private func decodeViaFFmpeg(inputURL: URL, outputWavURL: URL) throws -> Double {
    guard let ffmpeg = findExecutable("ffmpeg") else {
        let ext = inputURL.pathExtension.lowercased()
        throw SidecarError.decodeFailed(
            "macOS AVFoundation could not decode .\(ext) directly and ffmpeg was not found on PATH. "
                + "Convert to .mp4/.mov/.m4a/.mp3/.wav/.flac/.opus or install ffmpeg."
        )
    }

    // Check if container is video-only before invoking ffmpeg
    if let ffp = probeWithFFprobe(url: inputURL), !ffp.hasAudio {
        throw SidecarError.noAudioTrack("Media file contains no audio track")
    }

    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: ffmpeg)
    proc.arguments = [
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", inputURL.path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        outputWavURL.path,
    ]
    let errPipe = Pipe()
    proc.standardOutput = FileHandle.nullDevice
    proc.standardError = errPipe
    try proc.run()
    let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
    proc.waitUntilExit()
    guard proc.terminationStatus == 0 else {
        let errText = String(data: errData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if errText.localizedCaseInsensitiveContains("does not contain any stream")
            || errText.localizedCaseInsensitiveContains("matches no streams")
        {
            throw SidecarError.noAudioTrack("Media file contains no audio track")
        }
        throw SidecarError.decodeFailed("Audio decode failed: \(errText)")
    }

    let wavFile = try AVAudioFile(forReading: outputWavURL)
    let realDuration = Double(wavFile.length) / max(1.0, wavFile.processingFormat.sampleRate)
    if wavFile.length > 0 && wavFile.length < 16000 {
        let samples = try AudioConverter(sampleRate: 16000).resampleAudioFile(outputWavURL)
        let writer = try WavStreamWriter(url: outputWavURL, sampleRate: 16000)
        try samples.withUnsafeBufferPointer { ptr in
            try writer.appendFloatSamples(ptr)
        }
        try writer.padToMinimumSamples(16000)
        try writer.finish()
    }
    return realDuration
}

/// Decodes any supported audio/video file into a temporary 16 kHz mono PCM WAV file.
private func prepareNormalizedWav(for inputURL: URL) async throws -> (wavURL: URL, durationSec: Double, sampleCount: Int) {
    let tempURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("docprep-audio-\(ProcessInfo.processInfo.processIdentifier)-\(UUID().uuidString).wav")

    let ext = inputURL.pathExtension.lowercased()

    // 1. Ogg Opus native decoder
    if ext == "opus" || ext == "ogg" {
        if let dur = try? decodeOggOpusToWav(inputURL: inputURL, outputWavURL: tempURL) {
            let wavFile = try AVAudioFile(forReading: tempURL)
            return (tempURL, dur, Int(wavFile.length))
        }
    }

    // 2. AVAssetReader streaming decoder (handles .mp4, .mov, .m4a, .mp3, .wav, .aac, .flac, etc. in low memory)
    do {
        let dur = try await decodeViaAVAssetReader(inputURL: inputURL, outputWavURL: tempURL)
        let wavFile = try AVAudioFile(forReading: tempURL)
        return (tempURL, dur, Int(wavFile.length))
    } catch let err as SidecarError {
        if case .noAudioTrack = err {
            try? FileManager.default.removeItem(at: tempURL)
            throw err
        }
    } catch {}

    // 3. AVAudioFile fallback (e.g. certain FLAC/WAV variants)
    if let dur = try? decodeViaAVAudioFile(inputURL: inputURL, outputWavURL: tempURL) {
        let wavFile = try AVAudioFile(forReading: tempURL)
        return (tempURL, dur, Int(wavFile.length))
    }

    // 4. Local ffmpeg fallback for .webm / .mkv / Ogg Vorbis
    do {
        let dur = try decodeViaFFmpeg(inputURL: inputURL, outputWavURL: tempURL)
        let wavFile = try AVAudioFile(forReading: tempURL)
        return (tempURL, dur, Int(wavFile.length))
    } catch {
        try? FileManager.default.removeItem(at: tempURL)
        throw error
    }
}

// MARK: - Word-to-Speaker Alignment & Segment Construction

private struct AttributedWord {
    let word: String
    let start: Double
    let end: Double
    var rawSpeaker: String
}

private func assignSpeaker(
    wordStart: Double,
    wordEnd: Double,
    segments: [TimedSpeakerSegment]
) -> String {
    guard !segments.isEmpty else { return "S1" }

    var bestSpeaker = segments[0].speakerId
    var bestOverlap: Double = 0.0

    for seg in segments {
        let sStart = Double(seg.startTimeSeconds)
        let sEnd = Double(seg.endTimeSeconds)
        let overlap = min(wordEnd, sEnd) - max(wordStart, sStart)
        if overlap > bestOverlap {
            bestOverlap = overlap
            bestSpeaker = seg.speakerId
        }
    }

    if bestOverlap > 0 {
        return bestSpeaker
    }

    // Nearest segment fallback when word falls in a brief inter-segment boundary
    let mid = 0.5 * (wordStart + wordEnd)
    var minDist = Double.greatestFiniteMagnitude
    for seg in segments {
        let sStart = Double(seg.startTimeSeconds)
        let sEnd = Double(seg.endTimeSeconds)
        let dist: Double
        if mid < sStart {
            dist = sStart - mid
        } else if mid > sEnd {
            dist = mid - sEnd
        } else {
            dist = 0
        }
        if dist < minDist {
            minDist = dist
            bestSpeaker = seg.speakerId
        }
    }
    return bestSpeaker
}

private func buildDiarizedSegments(
    words: [WordTiming],
    fallbackText: String,
    durationSec: Double,
    speakerSegments: [TimedSpeakerSegment]
) -> (segments: [TranscriptSegment], speakerCount: Int) {
    if words.isEmpty {
        let trimmed = fallbackText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return ([], 0) }
        return (
            [
                TranscriptSegment(
                    start: 0.0,
                    end: max(0.0, durationSec),
                    speaker: "Speaker 1",
                    text: trimmed
                )
            ],
            1
        )
    }

    var attributed: [AttributedWord] = words.map { w in
        AttributedWord(
            word: w.word,
            start: max(0.0, min(w.startTime, durationSec > 0 ? durationSec : w.startTime)),
            end: max(w.startTime, min(w.endTime, durationSec > 0 ? durationSec : w.endTime)),
            rawSpeaker: assignSpeaker(wordStart: w.startTime, wordEnd: w.endTime, segments: speakerSegments)
        )
    }

    // Smooth isolated 1-word micro-flips (< 0.55s) sandwiched tightly between identical speakers
    if attributed.count >= 3 {
        for i in 1..<(attributed.count - 1) {
            let prev = attributed[i - 1]
            let curr = attributed[i]
            let next = attributed[i + 1]
            if prev.rawSpeaker == next.rawSpeaker
                && curr.rawSpeaker != prev.rawSpeaker
                && (curr.end - curr.start) < 0.55
                && (curr.start - prev.end) < 0.75
                && (next.start - curr.end) < 0.75
            {
                attributed[i].rawSpeaker = prev.rawSpeaker
            }
        }
    }

    // Normalize raw cluster IDs ("S2", "S1", ...) into chronological "Speaker 1", "Speaker 2", ...
    var speakerMap: [String: String] = [:]
    var nextSpeakerNumber = 1
    func canonicalSpeaker(_ raw: String) -> String {
        if let existing = speakerMap[raw] { return existing }
        let label = "Speaker \(nextSpeakerNumber)"
        nextSpeakerNumber += 1
        speakerMap[raw] = label
        return label
    }

    var out: [TranscriptSegment] = []
    var curSpeaker = canonicalSpeaker(attributed[0].rawSpeaker)
    var curStart = attributed[0].start
    var curEnd = attributed[0].end
    var curWords: [String] = [attributed[0].word]

    let pauseSplitThreshold = 3.0

    for item in attributed.dropFirst() {
        let spk = canonicalSpeaker(item.rawSpeaker)
        let gap = item.start - curEnd
        if spk != curSpeaker || gap > pauseSplitThreshold {
            let text = joinWordsCleanly(curWords)
            if !text.isEmpty {
                out.append(
                    TranscriptSegment(
                        start: roundMillis(curStart),
                        end: roundMillis(curEnd),
                        speaker: curSpeaker,
                        text: text
                    )
                )
            }
            curSpeaker = spk
            curStart = item.start
            curEnd = item.end
            curWords = [item.word]
        } else {
            curEnd = max(curEnd, item.end)
            curWords.append(item.word)
        }
    }

    let tailText = joinWordsCleanly(curWords)
    if !tailText.isEmpty {
        out.append(
            TranscriptSegment(
                start: roundMillis(curStart),
                end: roundMillis(curEnd),
                speaker: curSpeaker,
                text: tailText
            )
        )
    }

    return (out, speakerMap.count)
}

private func buildUndiarizedSegments(
    words: [WordTiming],
    fallbackText: String,
    durationSec: Double
) -> [TranscriptSegment] {
    if words.isEmpty {
        let trimmed = fallbackText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }
        return [
            TranscriptSegment(
                start: 0.0,
                end: roundMillis(max(0.0, durationSec)),
                speaker: nil,
                text: trimmed
            )
        ]
    }

    var out: [TranscriptSegment] = []
    var curStart = max(0.0, words[0].startTime)
    var curEnd = max(curStart, words[0].endTime)
    var curWords: [String] = [words[0].word]

    for w in words.dropFirst() {
        let gap = w.startTime - curEnd
        let prevWord = curWords.last ?? ""
        let endsSentence = prevWord.hasSuffix(".") || prevWord.hasSuffix("?") || prevWord.hasSuffix("!")
        let elapsed = curEnd - curStart

        if gap > 2.5 || (endsSentence && elapsed >= 15.0) || elapsed >= 45.0 {
            let text = joinWordsCleanly(curWords)
            if !text.isEmpty {
                out.append(
                    TranscriptSegment(
                        start: roundMillis(curStart),
                        end: roundMillis(curEnd),
                        speaker: nil,
                        text: text
                    )
                )
            }
            curStart = max(0.0, w.startTime)
            curEnd = max(curStart, w.endTime)
            curWords = [w.word]
        } else {
            curEnd = max(curEnd, w.endTime)
            curWords.append(w.word)
        }
    }

    let tailText = joinWordsCleanly(curWords)
    if !tailText.isEmpty {
        out.append(
            TranscriptSegment(
                start: roundMillis(curStart),
                end: roundMillis(curEnd),
                speaker: nil,
                text: tailText
            )
        )
    }
    return out
}

private func joinWordsCleanly(_ words: [String]) -> String {
    var result = ""
    let noSpaceBefore: Set<String> = [".", ",", "!", "?", ";", ":", "%", ")", "]", "}"]
    for w in words {
        let trimmed = w.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { continue }
        if result.isEmpty {
            result = trimmed
        } else if noSpaceBefore.contains(trimmed) {
            result += trimmed
        } else {
            result += " " + trimmed
        }
    }
    return result.trimmingCharacters(in: .whitespacesAndNewlines)
}

private func roundMillis(_ value: Double) -> Double {
    (value * 100.0).rounded() / 100.0
}

// MARK: - Download Mode (macOS CoreML Model Fetcher)

private func runDownload(modelsRoot: String, includeDiarizer: Bool) async throws {
    if let mirror = ProcessInfo.processInfo.environment["DOCPREP_MODEL_MIRROR_URL"],
        !mirror.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    {
        ModelRegistry.baseURL = mirror.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
    }

    let rootURL = URL(fileURLWithPath: modelsRoot, isDirectory: true)
    try FileManager.default.createDirectory(at: rootURL, withIntermediateDirectories: true)

    let asrTargetDir = rootURL.appendingPathComponent(Repo.parakeetV3.folderName, isDirectory: true)
    let lastPct = OSAllocatedUnfairLock(initialState: -1)

    @Sendable func unpackFiles(_ phase: DownloadPhase) -> (Int, Int) {
        if case .downloading(let completed, let total) = phase {
            return (completed, total)
        }
        return (0, 0)
    }

    emitJSON(DownloadEvent(stage: "asr", pct: 0, completed_files: 0, total_files: 0))

    _ = try await AsrModels.download(
        to: asrTargetDir,
        version: .v3,
        encoderPrecision: .int8,
        progressHandler: { prog in
            let scaled = includeDiarizer ? Int((prog.fractionCompleted * 88.0).rounded()) : Int((prog.fractionCompleted * 100.0).rounded())
            let shouldEmit = lastPct.withLock { prev -> Bool in
                if scaled > prev {
                    prev = scaled
                    return true
                }
                return false
            }
            if shouldEmit {
                let (done, total) = unpackFiles(prog.phase)
                emitJSON(
                    DownloadEvent(
                        stage: "asr",
                        pct: min(100, max(0, scaled)),
                        completed_files: done,
                        total_files: total
                    )
                )
            }
        }
    )

    if includeDiarizer {
        _ = try await ModelHub.download(
            .diarizer,
            to: rootURL,
            variant: "offline",
            progressHandler: { prog in
                let scaled = 88 + Int((prog.fractionCompleted * 12.0).rounded())
                let shouldEmit = lastPct.withLock { prev -> Bool in
                    if scaled > prev {
                        prev = scaled
                        return true
                    }
                    return false
                }
                if shouldEmit {
                    let (done, total) = unpackFiles(prog.phase)
                    emitJSON(
                        DownloadEvent(
                            stage: "diarizer",
                            pct: min(100, max(88, scaled)),
                            completed_files: done,
                            total_files: total
                        )
                    )
                }
            }
        )
    }

    emitJSON(DownloadEvent(stage: "complete", pct: 100, completed_files: 1, total_files: 1))
}

// MARK: - Transcribe Mode (Strictly Offline)

private func parseSpeakerConfig(_ hint: String) -> OfflineDiarizerConfig {
    let normalized = hint.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    let base = OfflineDiarizerConfig.default
    if normalized.isEmpty || normalized == "auto" {
        return base
    }
    if normalized == "4+" {
        return base.withSpeakers(min: 4, max: nil)
    }
    if let exact = Int(normalized), exact >= 1 {
        return base.withSpeakers(exactly: exact)
    }
    return base
}

private func runTranscribe(
    filePath: String,
    modelsRoot: String,
    diarize: Bool,
    speakersHint: String
) async throws {
    // Strictly block any runtime network access during conversion
    ModelHub.offlineMode = true

    let inputURL = URL(fileURLWithPath: filePath)
    guard FileManager.default.fileExists(atPath: inputURL.path) else {
        throw SidecarError.fileNotFound("Audio/video file not found: \(filePath)")
    }

    let rootURL = URL(fileURLWithPath: modelsRoot, isDirectory: true)
    let asrDir = rootURL.appendingPathComponent(Repo.parakeetV3.folderName, isDirectory: true)

    guard AsrModels.modelsExist(at: asrDir, version: .v3, encoderPrecision: .int8) else {
        throw SidecarError.modelMissing(
            "Speech model 'parakeet-tdt-0.6b-v3' is not installed in \(rootURL.path). "
                + "Download it in Settings -> Audio & Video or via --download-model parakeet-tdt-0.6b-v3."
        )
    }

    if diarize {
        let pldaCandidates = [
            rootURL.appendingPathComponent("\(Repo.diarizer.folderName)/plda-parameters.json"),
            rootURL.appendingPathComponent("speaker-diarization-offline/plda-parameters.json"),
            rootURL.appendingPathComponent("speaker-diarization-coreml/plda-parameters.json"),
        ]
        if !pldaCandidates.contains(where: { FileManager.default.fileExists(atPath: $0.path) }) {
            throw SidecarError.modelMissing(
                "Speaker diarization weights are missing in \(rootURL.path). "
                    + "Re-download 'parakeet-tdt-0.6b-v3' or pass --no-diarize."
            )
        }
    }

    emitJSON(ProgressEvent(stage: "decoding", pct: 0))
    let (wavURL, realDurationSec, sampleCount) = try await prepareNormalizedWav(for: inputURL)
    defer {
        try? FileManager.default.removeItem(at: wavURL)
    }

    if sampleCount == 0 || realDurationSec <= 0.01 {
        emitJSON(
            TranscribeResultEvent(
                duration_sec: 0.0,
                speakers: 0,
                segments: []
            )
        )
        return
    }

    emitJSON(ProgressEvent(stage: "transcribing", pct: 0))

    let asrModels: AsrModels
    do {
        asrModels = try await AsrModels.load(
            from: asrDir,
            version: .v3,
            encoderPrecision: .int8
        )
    } catch {
        throw SidecarError.modelMissing("Failed to load offline Parakeet CoreML models: \(error.localizedDescription)")
    }

    let asrManager = AsrManager(config: .default, models: asrModels)
    let progressStream = await asrManager.transcriptionProgressStream
    let progressTask = Task {
        let lastEmitted = OSAllocatedUnfairLock(initialState: 0)
        do {
            for try await frac in progressStream {
                let pct = min(99, max(1, Int((frac * 100.0).rounded())))
                let ok = lastEmitted.withLock { prev -> Bool in
                    if pct >= prev + 2 {
                        prev = pct
                        return true
                    }
                    return false
                }
                if ok {
                    emitJSON(ProgressEvent(stage: "transcribing", pct: pct))
                }
            }
        } catch {}
    }

    var decoderState = TdtDecoderState.make(decoderLayers: await asrManager.decoderLayerCount)
    let asrResult: ASRResult
    do {
        asrResult = try await asrManager.transcribe(wavURL, decoderState: &decoderState)
    } catch {
        progressTask.cancel()
        throw SidecarError.inferenceFailed("Transcription failed: \(error.localizedDescription)")
    }
    progressTask.cancel()
    emitJSON(ProgressEvent(stage: "transcribing", pct: 100))

    let words = buildWordTimings(from: asrResult.tokenTimings ?? [])

    if !diarize {
        let segments = buildUndiarizedSegments(
            words: words,
            fallbackText: asrResult.text,
            durationSec: realDurationSec
        )
        emitJSON(
            TranscribeResultEvent(
                duration_sec: roundMillis(realDurationSec),
                speakers: 0,
                segments: segments
            )
        )
        return
    }

    // Offline Speaker Diarization
    emitJSON(ProgressEvent(stage: "diarizing", pct: 0))
    let diarConfig = parseSpeakerConfig(speakersHint)
    let diarModels: OfflineDiarizerModels
    do {
        diarModels = try await OfflineDiarizerModels.load(from: rootURL)
    } catch {
        throw SidecarError.modelMissing("Failed to load offline diarizer CoreML models: \(error.localizedDescription)")
    }

    let diarManager = OfflineDiarizerManager(config: diarConfig)
    diarManager.initialize(models: diarModels)

    let lastDiarPct = OSAllocatedUnfairLock(initialState: 0)
    var speakerSegments: [TimedSpeakerSegment] = []
    do {
        let diarResult = try await diarManager.process(wavURL) { done, total in
            guard total > 0 else { return }
            let pct = min(95, max(1, Int((Double(done) / Double(total) * 95.0).rounded())))
            let ok = lastDiarPct.withLock { prev -> Bool in
                if pct >= prev + 5 {
                    prev = pct
                    return true
                }
                return false
            }
            if ok {
                emitJSON(ProgressEvent(stage: "diarizing", pct: pct))
            }
        }
        speakerSegments = diarResult.segments
    } catch OfflineDiarizationError.noSpeechDetected {
        speakerSegments = []
    } catch {
        throw SidecarError.inferenceFailed("Speaker diarization failed: \(error.localizedDescription)")
    }
    emitJSON(ProgressEvent(stage: "diarizing", pct: 100))

    let (segments, speakerCount) = buildDiarizedSegments(
        words: words,
        fallbackText: asrResult.text,
        durationSec: realDurationSec,
        speakerSegments: speakerSegments
    )

    emitJSON(
        TranscribeResultEvent(
            duration_sec: roundMillis(realDurationSec),
            speakers: speakerCount,
            segments: segments
        )
    )
}

// MARK: - CLI Argument Parsing & Entry Point

@main
struct DocPrepAudioCLI {
    static func main() async {
        // Keep FluidAudio internal logging quiet; stdout is strictly JSON-only
        AppLogger.minimumLevel = .error
        AppLogger.mirrorsToConsole = false

        let args = Array(CommandLine.arguments.dropFirst())
        guard let first = args.first else {
            emitJSON(
                ErrorEvent(
                    code: "invalid_args",
                    message: "Usage: docprep-audio [--hardware | --probe <files...> | --download --models-root <dir> | --transcribe <file> --models-root <dir>]"
                )
            )
            Foundation.exit(2)
        }

        do {
            switch first {
            case "--hardware":
                emitJSON(detectHardware())
                Foundation.exit(0)

            case "--probe":
                let paths = Array(args.dropFirst())
                if paths.isEmpty {
                    emitJSON([ProbeEntry]())
                    Foundation.exit(0)
                }
                var entries: [ProbeEntry] = []
                entries.reserveCapacity(paths.count)
                for p in paths {
                    entries.append(await probeSingleFile(p))
                }
                emitJSON(entries)
                Foundation.exit(0)

            case "--download":
                var modelsRoot: String?
                var includeDiarizer = true
                var i = 1
                while i < args.count {
                    switch args[i] {
                    case "--models-root":
                        i += 1
                        if i < args.count { modelsRoot = args[i] }
                    case "--model-id":
                        i += 1  // accepted for symmetry; Mac v1 ships parakeet-tdt-0.6b-v3
                    case "--no-diarizer":
                        includeDiarizer = false
                    case "--with-diarizer":
                        includeDiarizer = true
                    default:
                        break
                    }
                    i += 1
                }
                guard let root = modelsRoot, !root.isEmpty else {
                    throw SidecarError.invalidArgs("--download requires --models-root <dir>")
                }
                try await runDownload(modelsRoot: root, includeDiarizer: includeDiarizer)
                Foundation.exit(0)

            case "--transcribe":
                guard args.count >= 2 else {
                    throw SidecarError.invalidArgs("--transcribe requires <file>")
                }
                let filePath = args[1]
                var modelsRoot: String?
                var diarize = false
                var speakers = "auto"
                var i = 2
                while i < args.count {
                    switch args[i] {
                    case "--models-root":
                        i += 1
                        if i < args.count { modelsRoot = args[i] }
                    case "--model-id":
                        i += 1  // accepted for cross-platform CLI symmetry
                    case "--diarize":
                        diarize = true
                    case "--no-diarize":
                        diarize = false
                    case "--speakers":
                        i += 1
                        if i < args.count { speakers = args[i] }
                    default:
                        break
                    }
                    i += 1
                }
                guard let root = modelsRoot, !root.isEmpty else {
                    throw SidecarError.invalidArgs("--transcribe requires --models-root <dir>")
                }
                try await runTranscribe(
                    filePath: filePath,
                    modelsRoot: root,
                    diarize: diarize,
                    speakersHint: speakers
                )
                Foundation.exit(0)

            default:
                throw SidecarError.invalidArgs("Unknown flag: \(first)")
            }
        } catch let sidecarErr as SidecarError {
            emitJSON(ErrorEvent(code: sidecarErr.code, message: sidecarErr.localizedDescription))
            Foundation.exit(sidecarErr.exitCode)
        } catch {
            emitJSON(ErrorEvent(code: "runtime_error", message: error.localizedDescription))
            Foundation.exit(1)
        }
    }
}
