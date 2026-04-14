import json
import csv
import glob
import os
from datetime import datetime

def parse_journal_log(file_path: str):
    """
    Parses a single journal.py dump log file.
    Extracts relevant entry events to calculate empirical fill probability.
    """
    data = []
    with open(file_path, "r") as f:
        for line in f:
            if not line.strip(): continue
            try:
                event = json.loads(line)
            except Exception:
                continue
                
            kind = event.get("kind")
            if kind == "entry":
                # Extract features for ML
                row = {
                    "timestamp": event.get("timestamp"),
                    "market_slug": event.get("slug"),
                    "side": event.get("side"),
                    "entry_price": event.get("entry_price_usd"),
                    "signal_score": event.get("signal_score"),
                    "model_edge": event.get("model_edge"),
                    # Target variable: Was it executed as maker successfully? 
                    # 1 = true maker fill, 0 = missed (or forced taker fallback previously)
                    "fill_success": 1 if event.get("status") in ("filled", "partial") else 0,
                    "execution_style": event.get("execution_style", "unknown"),
                    "latency_detected": event.get("latency_ms", 0.0)
                }
                data.append(row)
    return data

def build_dataset(journal_dir: str, output_csv: str):
    """
    Scans journal logs and outputs a calibrated ML dataset.
    """
    print(f"Scanning {journal_dir} for journal logs...")
    all_data = []
    
    # Check both potential standard paths
    search_paths = [
        os.path.join(journal_dir, "*.jsonl"),
        os.path.join(journal_dir, "*.log")
    ]
    
    files = []
    for sp in search_paths:
        files.extend(glob.glob(sp))
        
    for f in files:
        all_data.extend(parse_journal_log(f))
        
    if not all_data:
        print("No data found!")
        return
        
    keys = all_data[0].keys()
    with open(output_csv, 'w', newline='') as output_file:
        dict_writer = csv.DictWriter(output_file, fieldnames=keys)
        dict_writer.writeheader()
        dict_writer.writerows(all_data)
        
    print(f"Exported {len(all_data)} records to {output_csv}.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=str, default="logs", help="Directory containing journal logs")
    parser.add_argument("--out", type=str, default="ml_dataset.csv", help="Output CSV path")
    args = parser.parse_args()
    
    build_dataset(args.dir, args.out)
