// ocr_vision.swift -- render PDF pages with PDFKit and recognise text with Apple's Vision framework.
//
// Build:  swiftc -O scripts/extract/ocr_vision.swift -o scripts/extract/bin/ocr_vision
// Usage:  ocr_vision <pdf> <first-page> <last-page> <out-dir> [--dpi 300] [--rotate 0|90|180|270] [--force]
//         ocr_vision <pdf> <page> -          (single page, JSON to stdout)
// Pages are 1-based.  One JSON file per page: <out-dir>/p0001.json
//
// JSON: {"page":N,"pdf_w":pts,"pdf_h":pts,"img_w":px,"img_h":px,"dpi":300,"rotate":deg,
//        "lines":[{"text":..,"conf":..,"x":..,"y":..,"w":..,"h":..,
//                  "words":[{"text":..,"x":..,"y":..,"w":..,"h":..}]}]}
// Coordinates are in image pixels with the origin at the TOP-LEFT of the rendered (and rotated) image.
// Recognition: VNRecognizeTextRequest, .accurate, language correction OFF (it "corrects" digits into words),
// minimum text height small so tiny table digits are kept.
import Foundation
import PDFKit
import Vision
import CoreGraphics

func render(_ page: PDFPage, dpi: Double, rotate: Int) -> CGImage? {
    let box = page.bounds(for: .mediaBox)
    var rot = page.rotation
    let swap = (rot % 180) != 0
    let pw = swap ? box.height : box.width
    let ph = swap ? box.width : box.height
    _ = rot; rot = 0
    let scale = dpi / 72.0
    let w0 = Int((pw * scale).rounded()), h0 = Int((ph * scale).rounded())
    let r = ((rotate % 360) + 360) % 360
    let (w, h) = (r == 90 || r == 270) ? (h0, w0) : (w0, h0)
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return nil }
    ctx.setFillColor(gray: 1, alpha: 1)
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.interpolationQuality = .high
    // rotate the drawing about the centre (clockwise by r degrees as seen on screen)
    ctx.translateBy(x: CGFloat(w) / 2, y: CGFloat(h) / 2)
    ctx.rotate(by: -CGFloat(r) * .pi / 180)
    ctx.translateBy(x: -CGFloat(w0) / 2, y: -CGFloat(h0) / 2)
    ctx.scaleBy(x: CGFloat(scale), y: CGFloat(scale))
    page.draw(with: .mediaBox, to: ctx)
    return ctx.makeImage()
}

func jstr(_ s: String) -> String {
    let d = try! JSONSerialization.data(withJSONObject: [s], options: [])
    let t = String(data: d, encoding: .utf8)!
    return String(t.dropFirst().dropLast())
}

func ocr(_ img: CGImage) -> [[String: Any]] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    req.minimumTextHeight = 0.004
    req.revision = VNRecognizeTextRequestRevision3
    let h = VNImageRequestHandler(cgImage: img, options: [:])
    try? h.perform([req])
    let W = Double(img.width), H = Double(img.height)
    var lines: [[String: Any]] = []
    for ob in req.results ?? [] {
        guard let cand = ob.topCandidates(1).first else { continue }
        let bb = ob.boundingBox
        var words: [[String: Any]] = []
        let s = cand.string
        var idx = s.startIndex
        while idx < s.endIndex {
            while idx < s.endIndex && s[idx] == " " { idx = s.index(after: idx) }
            if idx >= s.endIndex { break }
            var e = idx
            while e < s.endIndex && s[e] != " " { e = s.index(after: e) }
            if let wb = try? cand.boundingBox(for: idx..<e) {
                let b = wb.boundingBox
                words.append(["text": String(s[idx..<e]), "x": b.minX * W, "y": (1 - b.maxY) * H, "w": b.width * W, "h": b.height * H])
            }
            idx = e
        }
        lines.append(["text": s, "conf": Double(cand.confidence), "x": bb.minX * W, "y": (1 - bb.maxY) * H,
                      "w": bb.width * W, "h": bb.height * H, "words": words])
    }
    lines.sort { (a, b) in
        let ya = (a["y"] as! Double), yb = (b["y"] as! Double)
        if abs(ya - yb) > 1 { return ya < yb }
        return (a["x"] as! Double) < (b["x"] as! Double)
    }
    return lines
}

func pageJSON(_ n: Int, _ page: PDFPage, dpi: Double, rotate: Int) -> String? {
    guard let img = render(page, dpi: dpi, rotate: rotate) else { return nil }
    let box = page.bounds(for: .mediaBox)
    let lines = ocr(img)
    let ld = try! JSONSerialization.data(withJSONObject: lines, options: [.sortedKeys])
    return "{\"page\":\(n),\"pdf_w\":\(box.width),\"pdf_h\":\(box.height),\"img_w\":\(img.width),\"img_h\":\(img.height),\"dpi\":\(Int(dpi)),\"rotate\":\(rotate),\"lines\":" + String(data: ld, encoding: .utf8)! + "}"
}

var args = Array(CommandLine.arguments.dropFirst())
func opt(_ name: String) -> String? {
    if let i = args.firstIndex(of: name), i + 1 < args.count { let v = args[i + 1]; args.removeSubrange(i...(i + 1)); return v }
    return nil
}
let dpi = Double(opt("--dpi") ?? "300")!
let rotate = Int(opt("--rotate") ?? "0")!
let force = args.contains("--force"); args.removeAll { $0 == "--force" }
guard args.count >= 3, let doc = PDFDocument(url: URL(fileURLWithPath: args[0])) else {
    FileHandle.standardError.write("usage: ocr_vision <pdf> <first> <last|-> [<out-dir>] [--dpi N] [--rotate D] [--force]\n".data(using: .utf8)!)
    exit(2)
}
let first = Int(args[1])!
if args[2] == "-" {
    guard let p = doc.page(at: first - 1), let j = pageJSON(first, p, dpi: dpi, rotate: rotate) else { exit(1) }
    print(j)
    exit(0)
}
let last = Int(args[2])!
let outDir = args[3]
try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
for n in first...last {
    let path = String(format: "%@/p%04d.json", outDir, n)
    if !force && FileManager.default.fileExists(atPath: path) { continue }
    autoreleasepool {
        guard let p = doc.page(at: n - 1), let j = pageJSON(n, p, dpi: dpi, rotate: rotate) else {
            FileHandle.standardError.write("page \(n) failed\n".data(using: .utf8)!); return
        }
        try? j.write(toFile: path, atomically: true, encoding: .utf8)
    }
    FileHandle.standardError.write("p\(n)\n".data(using: .utf8)!)
}
