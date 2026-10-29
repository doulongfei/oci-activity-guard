"""持久化样本与执行预算；重启不会恢复已经消耗的额度。"""
import json
from pathlib import Path
import sqlite3


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.directory / 'metrics.db', timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples (start REAL NOT NULL, end REAL PRIMARY KEY, cpu REAL NOT NULL, steal REAL NOT NULL, cores INTEGER NOT NULL, memory_available INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, start REAL NOT NULL, heartbeat REAL NOT NULL, end REAL, limit_seconds INTEGER NOT NULL, calibration INTEGER NOT NULL, kind TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_start ON samples(start);
            CREATE INDEX IF NOT EXISTS runs_start ON runs(start);
        ''')

    def close(self):
        self.connection.close()

    def get(self, key, default=None):
        row = self.connection.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connection:
            self.connection.execute('INSERT INTO metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))

    def initialize(self, now):
        with self.connection:
            self.connection.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)', ('initialized_at', json.dumps(now)))
            self.connection.execute('INSERT OR IGNORE INTO metadata VALUES (?, ?)', ('paused', 'false'))

    def add_sample(self, sample):
        previous = self.connection.execute('SELECT MAX(end) FROM samples').fetchone()[0]
        if sample['end'] <= sample['start'] or not 0 <= sample['cpu'] <= 100 or not 0 <= sample['steal'] <= 100:
            raise ValueError('Invalid sample')
        if previous is not None and sample['start'] < previous - .01:
            raise ValueError('Overlapping or out-of-order sample')
        with self.connection:
            self.connection.execute('INSERT INTO samples VALUES (:start,:end,:cpu,:steal,:cores,:memory_available)', sample)

    def summary(self, now, seconds, threshold):
        rows = self.connection.execute('SELECT start,end,cpu FROM samples WHERE end>? AND start<? ORDER BY end', (now-seconds, now)).fetchall()
        weighted = []
        for row in rows:
            duration = max(0, min(now, row['end']) - max(now-seconds, row['start']))
            if duration:
                weighted.append((row['cpu'], duration))
        coverage = sum(weight for _, weight in weighted)
        eligible = sum(weight for value, weight in weighted if value >= threshold)
        rank = coverage * .95
        cumulative = 0
        percentile = None
        for value, weight in sorted(weighted):
            cumulative += weight
            if cumulative >= rank:
                percentile = value
                break
        return {'observed_seconds': round(coverage, 1), 'coverage_ratio': round(coverage/seconds, 5),
                'eligible_seconds': round(eligible, 1), 'eligible_ratio': round(eligible/seconds, 5),
                'observed_p95': round(percentile, 2) if percentile is not None else None}

    def run_start(self, now, seconds, calibration, kind='scheduled'):
        with self.connection:
            row = self.connection.execute('INSERT INTO runs(start,heartbeat,limit_seconds,calibration,kind,result,reason) VALUES (?,?,?,?,?,?,?)',
                                          (now, now, seconds, int(calibration), kind, 'running', 'started'))
        return row.lastrowid

    def heartbeat(self, run_id, now):
        with self.connection:
            self.connection.execute('UPDATE runs SET heartbeat=? WHERE id=? AND end IS NULL', (now, run_id))

    def finish(self, run_id, now, result, reason):
        with self.connection:
            self.connection.execute('UPDATE runs SET end=MAX(start,?),heartbeat=?,result=?,reason=? WHERE id=? AND end IS NULL', (now, now, result, reason, run_id))

    def reconcile(self, now):
        # 不知道中断后实际执行了多久时，按最大允许窗口保守记账；不计为有效 CPU 时间。
        with self.connection:
            self.connection.execute("UPDATE runs SET end=MAX(start,MIN(?,start+limit_seconds+15)), result='interrupted',reason='controller-restart' WHERE end IS NULL", (now,))

    def budget_used(self, now, kind=None):
        rows = self.connection.execute('SELECT start,end,limit_seconds FROM runs WHERE start<? AND (end IS NULL OR end>?) AND (? IS NULL OR kind=?)', (now, now-86400, kind, kind)).fetchall()
        return sum(max(0, min(now, row['end'] if row['end'] is not None else row['start']+row['limit_seconds']+15) - max(now-86400, row['start'])) for row in rows)

    def event(self, now, kind, reason):
        with self.connection:
            self.connection.execute('INSERT INTO events(at,kind,reason) VALUES (?,?,?)', (now, kind, reason))

    def recent_runs(self):
        return [dict(row) for row in self.connection.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 10')]

    def cleanup(self, now, days):
        with self.connection:
            for table, column in [('samples', 'end'), ('events', 'at')]:
                self.connection.execute(f'DELETE FROM {table} WHERE {column}<?', (now-days*86400,))
            self.connection.execute('DELETE FROM runs WHERE end<?', (now-30*86400,))
