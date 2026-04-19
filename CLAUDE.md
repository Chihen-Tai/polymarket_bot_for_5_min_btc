# CLAUDE.md

> Personal multi-project workspace at `/Applications/codes`.
> Each subdirectory is an **independent project** with its own tech stack — no shared build system at root.

---

## Project Map

| Directory | Type | Status | Quick Command |
|-----------|------|--------|---------------|
| `AI_agent/` | Multi-AI agent playground | ✅ Active | See `AI_agent/CLAUDE.md` |
| `polymarket-bot-by_openclaw/` | Python trading bot (Polymarket BTC) | ✅ Active | See `polymarket-bot-by_openclaw/CLAUDE.md` |
| `everything-claude-code/` | Claude Code plugin collection | ✅ Active | See `everything-claude-code/CLAUDE.md` |
| `nail-coach-flutter/` | Flutter nail-trim reminder app | ✅ Active | `flutter pub get && flutter run` |
| `GeminiTranslator/` | Minecraft NeoForge mod (Java 24) | ✅ Active | `./gradlew build` |
| `JavaDemo/` | Java 24 Maven demo | 🗄️ Archive | `mvn compile` |
| `chemwebsite/` | Static chemistry site | 🗄️ Archive | Open `index.html` |
| `chem.github.io/` | Chemistry GitHub Pages | 🗄️ Archive | Open `index.html` |
| `nthu-chemistry/` | NTHU chemistry dept site | 🗄️ Archive | Open `index.html` |
| `OpenClaw-Workspace/` | AI agent persistent workspace | ✅ Active | ⚠️ See warning below |
| `matlab/` | MATLAB homework/scripts | 🗄️ Archive | Run `.m` files in MATLAB |
| `test/` | Scratch/experiment files | 🗄️ Archive | Mixed (JS, HTML, Swift) |

> **Static chemistry sites** (`chemwebsite/`, `chem.github.io/`, `nthu-chemistry/`) are all the same pattern — just open `index.html` in a browser. No build step.

---

## Per-Project Commands

### `nail-coach-flutter`
- **Requires:** Flutter SDK ≥ 3.3.0
- **Key deps:** `shared_preferences`, `intl`
- **Structure:** `lib/{main.dart, models/, screens/, services/}`

```bash
cd nail-coach-flutter
flutter pub get
flutter run       # requires device/emulator
flutter test
```

---

### `GeminiTranslator` — Minecraft NeoForge Mod
- **Stack:** Java 24, Gradle, official Mojang mappings
- **Docs:** [NeoForged docs](https://docs.neoforged.net/)

```bash
cd GeminiTranslator
./gradlew build
./gradlew --refresh-dependencies   # if deps are missing
./gradlew clean
```

---

### `JavaDemo`
- **Stack:** Java 24, plain Maven (no frameworks)

```bash
cd JavaDemo
mvn compile
mvn exec:java -Dexec.mainClass="<ClassName>"
```

---

### `polymarket-bot-by_openclaw`
- **Stack:** Python (conda env `polymarket-bot`)
- **Default:** `DRY_RUN=true` — no real trades unless explicitly changed

```bash
cd polymarket-bot-by_openclaw
conda activate polymarket-bot
python main.py
pytest tests/                              # all tests
pytest tests/test_file.py::test_name       # single test
```

---

### `everything-claude-code`
- **Stack:** Node.js

```bash
cd everything-claude-code
node tests/run-all.js          # all tests
node tests/lib/utils.test.js   # single test
```

---

## Root-Level Scratch Files

These are one-off experiments — no build system, no tests.

| File | Purpose |
|------|---------|
| `1.py` | matplotlib market-cap chart → outputs `market_cap.png` |
| `1.ipynb` | Jupyter notebook |
| `1.cpp`, `test.cpp` | Standalone C++ (compile with `g++ <file>`) |
| `video_operator.sh` | Shell utility script |

---

## ⚠️ OpenClaw-Workspace — Handle with Care

This is a **persistent AI agent workspace**, not a conventional app. It uses file-based memory to maintain continuity across sessions.

**Critical files — do NOT delete or overwrite without understanding the agent's continuity model:**

- `SOUL.md` — agent identity/values
- `USER.md` — user profile/preferences
- `state/` — current agent state
- `memory/YYYY-MM-DD.md` — daily memory logs

Read `AGENTS.md` before modifying anything here.
