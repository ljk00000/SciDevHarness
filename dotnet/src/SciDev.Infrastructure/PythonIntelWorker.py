"""Private stdio worker for the WPF Python editor."""

import json
from pathlib import Path
import sys

stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
sys.stdout = sys.stderr


def send(value):
    body = json.dumps(value, ensure_ascii=True).encode("ascii")
    stdout.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stdout.write(body)
    stdout.flush()


def read():
    header = stdin.readline(80)
    if not header:
        return None
    prefix = b"Content-Length: "
    if not header.startswith(prefix) or stdin.readline(3) != b"\r\n":
        raise ValueError("Invalid frame")
    length = int(header[len(prefix):])
    if not 0 < length <= 4 * 1024 * 1024:
        raise ValueError("Invalid length")
    body = stdin.read(length)
    if len(body) != length:
        raise EOFError("Incomplete frame")
    return json.loads(body)


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def completions(script, line, column, source_line):
    items = []
    seen = set()
    prefix = None
    for item in script.complete(line, column, fuzzy=False):
        size = item.get_completion_prefix_length()
        start = column - size
        if start < 0:
            continue
        typed = source_line[start:column]
        replace_length = utf16_length(typed)
        insertion = typed + item.complete
        if not insertion or insertion in seen or len(insertion) > 240:
            continue
        if prefix is not None and replace_length != prefix:
            continue
        prefix = replace_length
        seen.add(insertion)
        signatures = item.get_signatures()
        items.append({
            "text": insertion,
            "kind": item.type,
            "detail": (signatures[0].to_string() if signatures else item.description)[:240],
        })
        if len(items) >= 50:
            break
    return {"prefix": prefix or 0, "items": items}


def definition(script, line, column, root):
    for item in script.goto(line, column, follow_imports=True):
        if item.module_path is None or item.line is None or item.column is None:
            continue
        try:
            relative = Path(item.module_path).relative_to(root)
        except ValueError:
            continue
        return {
            "path": relative.as_posix(),
            "line": item.line,
            "column": utf16_length(item.get_line_code()[:item.column]) + 1,
        }
    return None


def main():
    try:
        import jedi
        root = Path.cwd()
        # Use the already isolated interpreter's import path. Jedi's helper
        # can report a different path for a Windows virtual environment.
        project = jedi.Project(root, load_unsafe_extensions=False, sys_path=list(sys.path))
    except ImportError:
        send({"status": "missing_jedi"})
        return
    except Exception:
        send({"status": "initialization_failed"})
        return

    send({"status": "ready", "version": jedi.__version__})
    while (request := read()) is not None:
        try:
            path = Path(request["path"])
            path.relative_to(root)
            source = request["text"].replace("\r\n", "\n").replace("\r", "\n")
            line = request["line"]
            source_line = source.split("\n")[line - 1]
            # AvalonEdit reports UTF-16 offsets. Jedi expects Unicode code points.
            column = len(source_line.encode("utf-16-le")[:request["column"] * 2].decode("utf-16-le"))
            script = jedi.Script(source, path=path, project=project)
            if request["operation"] == "complete":
                result = completions(script, line, column, source_line)
            elif request["operation"] == "definition":
                result = definition(script, line, column, root)
            else:
                raise ValueError("Unknown operation")
            send({"status": "ok", "result": result})
        except Exception:
            # Keep all source, paths and tool diagnostics out of the host log.
            send({"status": "analysis_failed"})


main()
