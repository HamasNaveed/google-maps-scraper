import os
import sys
import subprocess
import time
import csv
import io

# Force stdout to UTF-8 for Windows console support
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

QUERIES_FILE = "queries.txt"
COMPLETED_FILE = "completed_queries.txt"
RESULTS_FILE = "results.csv"
MAX_LEADS_PER_QUERY = 30

def check_results_file_unlocked():
    if not os.path.exists(RESULTS_FILE):
        return True
    try:
        with open(RESULTS_FILE, "a", encoding="utf-8"):
            pass
        return True
    except PermissionError:
        return False

def append_stdout_results_into_master(stdout_data, query):
    """
    Safely parses CSV output from scraper stdout in memory, takes up to MAX 30 leads per query,
    prints each added lead name cleanly to terminal, and appends into master results.csv.
    """
    if not stdout_data:
        return 0

    # Filter out empty lines
    lines = [line for line in stdout_data.splitlines(keepends=True) if line.strip()]
    if not lines:
        return 0

    header_line = lines[0]
    data_rows = lines[1:]

    if not data_rows:
        return 0

    # Cap at maximum 30 leads per query
    data_rows = data_rows[:MAX_LEADS_PER_QUERY]

    # Detect title column index in CSV header (default index 2)
    title_idx = 2
    try:
        reader = csv.reader(io.StringIO(header_line))
        header_fields = next(reader)
        if "title" in header_fields:
            title_idx = header_fields.index("title")
    except Exception:
        pass

    # Print each added lead cleanly to terminal
    for row_line in data_rows:
        try:
            row_fields = next(csv.reader(io.StringIO(row_line)))
            lead_name = row_fields[title_idx] if title_idx < len(row_fields) else "Unknown"
        except Exception:
            lead_name = "Unknown"
        print(f"  + New lead name added for query \"{query}\": {lead_name}")

    # Check if master results.csv exists and has content
    master_exists = os.path.exists(RESULTS_FILE) and os.path.getsize(RESULTS_FILE) > 0

    lines_to_append = []
    if not master_exists:
        # Write header first, then data rows
        lines_to_append = [header_line] + data_rows
    else:
        # Append data rows only (skip duplicate header)
        lines_to_append = data_rows

    if lines_to_append:
        with open(RESULTS_FILE, "a", encoding="utf-8") as master:
            master.writelines(lines_to_append)

    return len(data_rows)

def load_queries():
    if not os.path.exists(QUERIES_FILE):
        print(f"Error: {QUERIES_FILE} not found!")
        return []
    with open(QUERIES_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def load_completed():
    if not os.path.exists(COMPLETED_FILE):
        return set()
    with open(COMPLETED_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def mark_completed(query):
    with open(COMPLETED_FILE, "a", encoding="utf-8") as f:
        f.write(query + "\n")

def main():
    queries = load_queries()
    completed = load_completed()
    pending = [q for q in queries if q not in completed]

    total = len(queries)
    done_count = len(completed)
    remaining = len(pending)

    print("=" * 60)
    print("  Google Maps Scraper - Smart Batch Runner")
    print(f"  Total queries: {total}")
    print(f"  Already completed: {done_count}")
    print(f"  Remaining to scrape: {remaining}")
    print("=" * 60)

    if not pending:
        print("\nAll queries in queries.txt have already been completed!")
        print("To start over, delete 'completed_queries.txt' and 'results.csv'.")
        return

    # Ensure master results.csv file is not locked by Excel before starting
    while not check_results_file_unlocked():
        print(f"\n[WARNING] {RESULTS_FILE} is currently OPEN in another program (like Excel).")
        print(f"Please CLOSE {RESULTS_FILE} so the scraper can append leads to it.")
        print("Retrying in 5 seconds... (Press Ctrl+C to cancel)")
        time.sleep(5)

    for idx, query in enumerate(pending, start=1):
        while not check_results_file_unlocked():
            print(f"\n[WARNING] {RESULTS_FILE} is locked by Excel/editor. Please close it.")
            time.sleep(5)

        print(f"\n[{idx}/{remaining}] Processing: \"{query}\"...")

        cmd = [
            ".\\google_maps_scraper.exe",
            "-input", "stdin",
            "-results", "stdout",
            "-c", "4",
            "-pages-per-browser", "4",
            "-depth", "2",
            "-exit-on-inactivity", "1m"
        ]

        try:
            # Run scraper executable for this query using stdin/stdout in memory
            proc = subprocess.run(
                cmd,
                input=query + "\n",
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace"
            )

            # Safely merge up to 30 leads into master results.csv directly from stdout
            added_count = append_stdout_results_into_master(proc.stdout, query)

            if added_count > 0:
                mark_completed(query)
                print(f"[DONE] \"{query}\" -> Appended {added_count} leads to {RESULTS_FILE} & marked completed.")
            else:
                print(f"[RETRY NEEDED] \"{query}\" -> 0 leads extracted. Query NOT marked completed.")

        except KeyboardInterrupt:
            print("\n\n[PAUSED] Stopped by user! Progress saved in completed_queries.txt.")
            print("Run 'python run_scraper.py' anytime to resume where you left off.")
            sys.exit(0)
        except Exception as e:
            print(f"Error processing query \"{query}\": {e}")

    print("\n🎉 All remaining queries finished successfully!")

if __name__ == "__main__":
    main()
