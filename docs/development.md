# Development

```bash
uv sync
uv run pytest            # synthetic audio only, no sound device needed
uv run pytest -m live    # plays a test sound and detects it through loopback
uv run ruff check src tests examples && uv run basedpyright
```

Tests never use real recordings: game and application sound effects are usually copyrighted, so
keep your own clips out of the repository (`*.wav` is ignored).

Windows per-application capture is tested on every platform against a simulated Windows
(`tests/test_windows_app_capture.py`), and so are its ctypes bindings, against fake DLLs and COM
objects made in Python (`tests/test_wasapi_fake.py`). `tests/test_wasapi.py` runs them against
Windows itself, on Windows only, without needing a sound device; see step 6 of
[Testing on Windows](windows-testing.md).

The code layout is described under [Architecture](how-it-works.md#architecture).
