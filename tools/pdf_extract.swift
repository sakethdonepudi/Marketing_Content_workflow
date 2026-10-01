import Foundation
import PDFKit

guard CommandLine.arguments.count == 2,
      let document = PDFDocument(url: URL(fileURLWithPath: CommandLine.arguments[1])) else {
    fputs("Unable to open PDF\n", stderr)
    exit(1)
}

var pages: [[String: Any]] = []
for index in 0..<document.pageCount {
    if let text = document.page(at: index)?.string, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
        pages.append(["page": index + 1, "text": text])
    }
}
let attributes = document.documentAttributes ?? [:]
var metadata: [String: String] = [:]
for (key, value) in attributes {
    metadata[String(describing: key)] = String(describing: value)
}
let payload: [String: Any] = ["pages": pages, "metadata": metadata]
let data = try JSONSerialization.data(withJSONObject: payload, options: [])
FileHandle.standardOutput.write(data)
