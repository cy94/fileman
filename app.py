import os
import shutil
import mimetypes
import yaml
from pathlib import Path
from flask import Flask, jsonify, request, send_file, abort, render_template, Response
from urllib.request import urlopen
from PIL import Image
import io


def load_config() -> dict:
	"""
	Load configuration from config.yaml, providing sensible defaults if missing.
	"""
	config_path = Path(__file__).parent / "config.yaml"
	with open(config_path, "r", encoding="utf-8") as f:
		cfg = yaml.safe_load(f) or {}
	allowed = cfg.get("allowed_roots") or []
	return {"allowed_roots": allowed}


MAX_TEXT_PREVIEW_BYTES = 512 * 1024  # 512 KiB cap for inline previews


def create_app() -> Flask:
	app = Flask(__name__, static_folder="static", template_folder="templates")

	cfg = load_config()

	def ensure_bootstrap_local() -> None:
		"""
		Ensure a local copy of Bootstrap CSS exists for offline environments.
		No error is raised if download fails; the UI has CDN fallbacks.
		"""
		static_dir = Path(app.static_folder or "static")
		target = static_dir / "bootstrap.min.css"
		if target.exists():
			return
		try:
			static_dir.mkdir(parents=True, exist_ok=True)
			with urlopen("https://unpkg.com/bootstrap@5.3.3/dist/css/bootstrap.min.css", timeout=5) as resp:
				content = resp.read()
			with open(target, "wb") as f:
				f.write(content)
		except Exception:
			# Silent fallback; the page will attempt other CDNs
			pass

	# Best-effort: try to fetch a local Bootstrap copy at startup
	ensure_bootstrap_local()

	def get_allowed_roots() -> list[str]:
		"""
		Load allowed roots from config on-demand to reflect runtime changes.
		"""
		cfg_now = load_config()
		return [str(Path(p).resolve()) for p in cfg_now.get("allowed_roots", [])]

	def is_within_root(path_str: str, root_str: str) -> bool:
		"""
		Validate that path_str is within the provided root_str.
		Uses pathlib parent traversal to correctly handle '/' and nested paths.
		"""
		try:
			real_path = Path(path_str).resolve()
			real_root = Path(root_str).resolve()
			if real_path == real_root:
				return True
			# real_root is an ancestor of real_path
			return real_root in real_path.parents
		except Exception:
			return False

	def coerce_path(p: str, default_root: str) -> str:
		# Empty path -> default root
		if not p:
			return default_root
		# If it's relative, join to default root
		candidate = Path(p)
		if not candidate.is_absolute():
			return str((Path(default_root) / candidate).resolve())
		return str(candidate.resolve())

	def list_directory(path_str: str) -> dict:
		path = Path(path_str)
		if not path.exists():
			abort(404, description="Path not found")
		if not path.is_dir():
			abort(400, description="Path is not a directory")
		entries = []
		try:
			with os.scandir(path) as it:
				for entry in it:
					try:
						stat = entry.stat(follow_symlinks=False)
						size = stat.st_size
						is_dir = entry.is_dir(follow_symlinks=False)
						mime, _ = mimetypes.guess_type(entry.name)
						# Fallback for systems with incomplete MIME databases
						if not mime:
							ext = Path(entry.name).suffix.lower()
							if ext in (".mp4", ".webm", ".ogg", ".mov", ".mkv", ".avi"):
								mime = "video/mp4" if ext == ".mp4" else f"video/{ext[1:]}"
							elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"):
								mime = f"image/{'jpeg' if ext in ('.jpg', '.jpeg') else ext[1:]}"
						is_image = (not is_dir) and isinstance(mime, str) and mime.startswith("image/")
						is_video = (not is_dir) and isinstance(mime, str) and mime.startswith("video/")
						entries.append({
							"name": entry.name,
							"is_dir": is_dir,
							"size": size,
							"is_image": is_image,
							"is_video": is_video,
							"mime": mime,
							"mtime": stat.st_mtime
						})
					except Exception:
						# Skip unreadable entries
						continue
		except PermissionError:
			abort(403, description="Permission denied")
		# Sort: directories first, then files; alphabetical within groups
		entries.sort(key=lambda e: (0 if e["is_dir"] else 1, e["name"].lower()))
		return {
			"path": str(path.resolve()),
			"entries": entries
		}

	@app.route("/")
	def index():
		# Redirect to UI
		return render_template("index.html")

	@app.get("/api/config")
	def get_config():
		# These are suggested defaults; the client can provide any absolute root
		return jsonify({"allowed_roots": get_allowed_roots()})

	@app.get("/api/list")
	def api_list():
		roots = get_allowed_roots()
		root = request.args.get("root") or (roots[0] if roots else "/")
		path_param = request.args.get("path") or ""
		# Compute target path
		target = coerce_path(path_param, root)
		# Root must exist and be a directory
		root_path = Path(root)
		if not root_path.exists() or not root_path.is_dir():
			abort(400, description="Root must be an existing directory")
		# Enforce containment within chosen root
		if not is_within_root(target, root):
			abort(403, description="Path is outside the chosen root")
		data = list_directory(target)
		# Also include parent if available within root
		parent = str(Path(target).parent.resolve())
		data["parent"] = parent if parent != str(Path(target).resolve()) and is_within_root(parent, root) else None
		return jsonify(data)

	def resolve_requested_file() -> Path:
		roots = get_allowed_roots()
		root = request.args.get("root") or (roots[0] if roots else "/")
		path_param = request.args.get("path")
		if not path_param:
			abort(400, description="Missing path")
		target = coerce_path(path_param, root)
		# Root must exist and be a directory
		root_path = Path(root)
		if not root_path.exists() or not root_path.is_dir():
			abort(400, description="Root must be an existing directory")
		# Enforce containment within chosen root
		if not is_within_root(target, root):
			abort(403, description="Path is outside the chosen root")
		return Path(target)

	@app.get("/api/file")
	def api_file():
		path = resolve_requested_file()
		if not path.exists() or not path.is_file():
			abort(404, description="File not found")
		mime, _ = mimetypes.guess_type(str(path))
		# Ensure video MIME for common extensions (mimetypes may miss some on minimal systems)
		if not mime and path.suffix.lower() in (".mp4", ".webm", ".ogg", ".mov"):
			mime = "video/mp4" if path.suffix.lower() == ".mp4" else f"video/{path.suffix[1:]}"
		mime = mime or "application/octet-stream"

		file_size = path.stat().st_size
		range_header = request.headers.get("Range")

		if range_header:
			# Parse "Range: bytes=start-end" or "bytes=-last" (suffix-byte-range)
			try:
				byte_range = range_header.replace("bytes=", "").strip()
				start_str, _, end_str = byte_range.partition("-")
				start_str = start_str.strip()
				end_str = end_str.strip()
				if start_str and end_str:
					start = int(start_str)
					end = min(int(end_str), file_size - 1)
				elif start_str:
					start = int(start_str)
					end = file_size - 1
				elif end_str:
					# Suffix range: last N bytes (e.g. bytes=-500)
					n = int(end_str)
					start = max(0, file_size - n)
					end = file_size - 1
				else:
					abort(416)
				start = max(0, start)
				end = min(end, file_size - 1)
				if start > end:
					abort(416)
				length = end - start + 1
			except (ValueError, AttributeError):
				abort(416)

			def generate():
				with open(path, "rb") as f:
					f.seek(start)
					remaining = length
					chunk = 65536
					while remaining > 0:
						data = f.read(min(chunk, remaining))
						if not data:
							break
						remaining -= len(data)
						yield data

			headers = {
				"Content-Range": f"bytes {start}-{end}/{file_size}",
				"Accept-Ranges": "bytes",
				"Content-Length": str(length),
				"Content-Type": mime,
			}
			return Response(generate(), status=206, headers=headers, direct_passthrough=True)

		# Non-range request: serve whole file but advertise range support
		resp = send_file(path, mimetype=mime, as_attachment=False)
		resp.headers["Accept-Ranges"] = "bytes"
		return resp

	@app.get("/api/download")
	def api_download():
		path = resolve_requested_file()
		if not path.exists() or not path.is_file():
			abort(404, description="File not found")
		mime, _ = mimetypes.guess_type(str(path))
		if not mime and path.suffix.lower() in (".mp4", ".webm", ".ogg", ".mov"):
			mime = "video/mp4" if path.suffix.lower() == ".mp4" else f"video/{path.suffix[1:]}"
		return send_file(path, mimetype=mime or "application/octet-stream", as_attachment=True, download_name=path.name)

	@app.get("/api/text_preview")
	def api_text_preview():
		path = resolve_requested_file()
		if not path.exists() or not path.is_file():
			abort(404, description="File not found")
		try:
			with path.open("rb") as handle:
				raw = handle.read(MAX_TEXT_PREVIEW_BYTES + 1)
		except OSError as exc:
			abort(500, description=f"Unable to read file: {exc}")
		# Decode as UTF-8, replacing invalid bytes so the UI always receives text
		text = raw.decode("utf-8", errors="replace")
		truncated = len(raw) > MAX_TEXT_PREVIEW_BYTES
		return jsonify({
			"path": str(path),
			"content": text,
			"encoding": "utf-8",
			"truncated": truncated,
			"max_bytes": MAX_TEXT_PREVIEW_BYTES
		})

	@app.get("/api/thumbnail")
	def api_thumbnail():
		path = resolve_requested_file()
		if not path.exists() or not path.is_file():
			abort(404, description="File not found")
		
		mime, _ = mimetypes.guess_type(str(path))
		if not mime or not mime.startswith("image/"):
			abort(400, description="File is not an image")
		
		try:
			# Get size parameter (default 48px)
			size = int(request.args.get("size", "48"))
			size = max(16, min(128, size))  # Clamp between 16 and 128
			
			# Open and resize image
			with Image.open(path) as img:
				# Convert to RGB if necessary (handles RGBA, P mode, etc.)
				if img.mode in ('RGBA', 'LA', 'P'):
					# Create white background for transparency
					background = Image.new('RGB', img.size, (255, 255, 255))
					if img.mode == 'P':
						img = img.convert('RGBA')
					if img.mode in ('RGBA', 'LA'):
						background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
						img = background
					else:
						img = img.convert('RGB')
				elif img.mode != 'RGB':
					img = img.convert('RGB')
				
				# Calculate thumbnail size maintaining aspect ratio
				img.thumbnail((size, size), Image.Resampling.LANCZOS)
				
				# Save to bytes
				output = io.BytesIO()
				img.save(output, format='JPEG', quality=85, optimize=True)
				output.seek(0)
				
				return Response(output.read(), mimetype='image/jpeg')
		except Exception as e:
			abort(500, description=f"Unable to generate thumbnail: {str(e)}")

	@app.post("/api/delete")
	def api_delete():
		roots = get_allowed_roots()
		payload = request.get_json(silent=True) or {}
		root = payload.get("root") or request.args.get("root") or (roots[0] if roots else "/")
		root_path = Path(root)
		if not root_path.exists() or not root_path.is_dir():
			abort(400, description="Root must be an existing directory")

		raw_paths = payload.get("paths")
		if raw_paths is None:
			single = payload.get("path") or request.args.get("path")
			if not single:
				abort(400, description="Missing path or paths")
			raw_paths = [single]
		if not isinstance(raw_paths, list) or not raw_paths:
			abort(400, description="paths must be a non-empty list")

		results = []
		for raw in raw_paths:
			if not isinstance(raw, str) or not raw:
				results.append({"path": raw, "ok": False, "error": "Invalid path"})
				continue
			target = coerce_path(raw, root)
			if not is_within_root(target, root):
				results.append({"path": raw, "ok": False, "error": "Path is outside the chosen root"})
				continue
			if Path(target).resolve() == root_path.resolve():
				results.append({"path": raw, "ok": False, "error": "Refusing to delete the root directory"})
				continue
			path = Path(target)
			if not path.exists() and not path.is_symlink():
				results.append({"path": raw, "ok": False, "error": "Path not found"})
				continue
			try:
				if path.is_symlink() or path.is_file():
					path.unlink()
				elif path.is_dir():
					shutil.rmtree(path)
				else:
					results.append({"path": raw, "ok": False, "error": "Unsupported path type"})
					continue
				results.append({"path": str(path), "ok": True})
			except PermissionError:
				results.append({"path": raw, "ok": False, "error": "Permission denied"})
			except OSError as exc:
				results.append({"path": raw, "ok": False, "error": f"Delete failed: {exc}"})

		ok_count = sum(1 for r in results if r["ok"])
		fail_count = len(results) - ok_count
		return jsonify({"results": results, "ok_count": ok_count, "fail_count": fail_count})

	return app


if __name__ == "__main__":
	app = create_app()
	# Bind to 0.0.0.0 for remote access; use PORT env or default 5000
	port = int(os.environ.get("PORT", "5000"))
	app.run(host="0.0.0.0", port=port, debug=True)


