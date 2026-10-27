// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "docprep-audio",
    platforms: [
        .macOS(.v14)
    ],
    dependencies: [
        // Opt out of TTS traits (`NemoTextProcessing` 87 MB xcframework) —
        // docprep-audio only uses Parakeet CoreML ASR + Offline Diarization.
        .package(url: "https://github.com/FluidInference/FluidAudio.git", from: "0.17.0", traits: [])
    ],
    targets: [
        .executableTarget(
            name: "docprep-audio",
            dependencies: [
                .product(name: "FluidAudio", package: "FluidAudio")
            ]
        )
    ]
)
