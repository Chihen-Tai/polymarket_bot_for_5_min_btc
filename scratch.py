import re
with open('/Applications/codes/polymarket-bot-by_openclaw/data/log-dryrun-2026-04-14T16-08-02.txt') as f:
    for line in f:
        m = re.search(r'up=([0-9\.]+) down=([0-9\.]+)', line)
        if m:
            up = float(m.group(1))
            down = float(m.group(2))
            s = up + down
            if abs(s - 1.0) > 0.05:
                print(f"Abnormal sum {s:.3f}: {line.strip()}")
