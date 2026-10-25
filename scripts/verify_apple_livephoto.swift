// Local macOS validation only; does not request access to or write a Photos library.
// Usage: swift verify_apple_livephoto.swift still.jpg paired.MOV expected_seconds
import AppKit
import Foundation
import Photos
import AVFoundation
import ImageIO
let args = CommandLine.arguments
if args.count != 4 { fputs("Expected still MOV seconds\n", stderr); exit(2) }
let imageURL = URL(fileURLWithPath: args[1]), movieURL = URL(fileURLWithPath: args[2])
let expected = Double(args[3])!
var result: [String: Any] = [:]
if let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
   let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any],
   let maker = properties[kCGImagePropertyMakerAppleDictionary as String] as? [String: Any] {
    result["image_identifier"] = maker["17"]
}
let asset = AVURLAsset(url: movieURL)
for entry in asset.metadata {
    if entry.key as? String == "com.apple.quicktime.content.identifier" {
        result["video_identifier"] = entry.stringValue
    }
}
var markerTimes: [Double] = []
for track in asset.tracks(withMediaType: .metadata) {
    do {
        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: nil)
        reader.add(output)
        let adaptor = AVAssetReaderOutputMetadataAdaptor(assetReaderTrackOutput: output)
        if reader.startReading() {
            while let group = adaptor.nextTimedMetadataGroup() {
                for item in group.items {
                    if item.key as? String == "com.apple.quicktime.still-image-time" {
                        markerTimes.append(group.timeRange.start.seconds)
                        result["marker_value"] = item.numberValue
                    }
                }
            }
        }
        if let error = reader.error { result["reader_error"] = error.localizedDescription }
    } catch { result["reader_error"] = error.localizedDescription }
}
result["marker_times_seconds"] = markerTimes
var done = false
PHLivePhoto.request(withResourceFileURLs: [imageURL, movieURL], placeholderImage: nil,
                    targetSize: .zero, contentMode: .aspectFit) { photo, info in
    if !(info[PHLivePhotoInfoIsDegradedKey] as? Bool ?? false) {
        result["apple_livephoto_valid"] = photo != nil
        if let error = info[PHLivePhotoInfoErrorKey] as? NSError { result["photos_error"] = error.description }
        done = true
    }
}
let deadline = Date().addingTimeInterval(30)
while !done && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.1)) }
let matched = (result["image_identifier"] as? String) != nil &&
    (result["image_identifier"] as? String) == (result["video_identifier"] as? String)
let timeMatches = markerTimes.count == 1 && abs(markerTimes[0] - expected) < 0.001
result["identifiers_match"] = matched
result["selected_frame_time_matches"] = timeMatches
result["does_not_import_into_photo_library"] = true
let passed = done && matched && timeMatches && result["apple_livephoto_valid"] as? Bool == true
result["status"] = passed ? "passed" : "failed"
let json = try! JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
print(String(data: json, encoding: .utf8)!)
exit(passed ? 0 : 1)
