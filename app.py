import os
import mimetypes
import yaml
from pathlib import Path
from flask import Flask, jsonify, request, send_file, abort, render_template
from urllib.request import urlopen


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
						is_image = (not is_dir) and isinstance(mime, str) and mime.startswith("image/")
						entries.append({
							"name": entry.name,
							"is_dir": is_dir,
							"size": size,
							"is_image": is_image,
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
		return send_file(path, mimetype=mime or "application/octet-stream", as_attachment=False, conditional=True)

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

	return app


if __name__ == "__main__":
	app = create_app()
	# Bind to 0.0.0.0 for remote access; use PORT env or default 5000
	port = int(os.environ.get("PORT", "5000"))
	app.run(host="0.0.0.0", port=port, debug=False)


