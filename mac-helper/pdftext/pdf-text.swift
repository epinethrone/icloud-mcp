// pdf-text: print the text of one PDF, page by page, using Apple's PDFKit. Used by ops/drive.py for drive_read.
// usage: pdf-text <file.pdf>   ->  text on stdout (pages separated by a form feed), a short reason on stderr and exit 1 on failure.
import Foundation
import PDFKit

let args = CommandLine.arguments
guard args.count == 2 else {
    FileHandle.standardError.write("usage: pdf-text <file.pdf>\n".data(using: .utf8)!)
    exit(2)
}
guard let doc = PDFDocument(url: URL(fileURLWithPath: args[1])) else {
    FileHandle.standardError.write("not a readable PDF\n".data(using: .utf8)!)
    exit(1)
}
if doc.isLocked {
    FileHandle.standardError.write("the PDF is password protected\n".data(using: .utf8)!)
    exit(1)
}
var out = ""
for i in 0..<doc.pageCount {
    if let page = doc.page(at: i), let text = page.string {
        if i > 0 { out += "\u{0C}\n" }
        out += text
    }
}
if out.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && doc.pageCount > 0 {
    FileHandle.standardError.write("this PDF has no text layer (probably a scan)\n".data(using: .utf8)!)
    exit(1)
}
FileHandle.standardOutput.write(out.data(using: .utf8)!)
