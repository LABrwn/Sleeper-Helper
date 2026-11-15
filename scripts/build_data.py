#!/usr/bin/env python3
"""Baut kompakte NFL-Daten für den Sleeper Helper.

Quelle sind die offenen Dateien des nflverse-Projekts (https://github.com/nflverse/nflverse-data).
Das Skript nutzt nur die Python-Standardbibliothek, damit im GitHub-Workflow nichts nachinstalliert
werden muss. Ausgabe sind kleine JSON-Dateien im Ordner data/, die die Webseite von der eigenen
Adresse lädt.

    python3 scripts/build_data.py --out data            # lädt die aktuellen Dateien
    python3 scripts/build_data.py --out data --cache c  # nutzt bereits geladene Dateien in c/
"""
import re
import argparse, csv, datetime, gzip, io, json, math, os, sys, time, urllib.request
from collections import defaultdict

BASE = 'https://github.com/nflverse/nflverse-data/releases/download/'
POS = ('QB', 'RB', 'WR', 'TE')
TEAM_FIX = {'LA': 'LAR', 'STL': 'LAR', 'SD': 'LAC', 'OAK': 'LV'}   # nflverse-Kürzel -> Sleeper-Kürzel
csv.field_size_limit(10 ** 8)


def team(t):
    return TEAM_FIX.get(t, t) if t else ''


def num(v, d=0.0):
    try:
        return float(v) if v not in ('', 'NA', None) else d
    except ValueError:
        return d


def fetch(rel, cache):
    """Lädt eine Release-Datei (mit Wiederholungen) und gibt den lokalen Pfad zurück."""
    name = rel.split('/')[-1]
    path = os.path.join(cache, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(cache, exist_ok=True)
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(BASE + rel, headers={'User-Agent': 'sleeper-helper-data/1.0'})
            with urllib.request.urlopen(req, timeout=120) as r, open(path + '.part', 'wb') as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(path + '.part', path)
            return path
        except Exception as e:  # Netzfehler: kurz warten, nochmal versuchen
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f'Download fehlgeschlagen: {rel}: {last}')


def rows(path):
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt', newline='', encoding='utf-8') as f:
        yield from csv.DictReader(f)


def pick_season(games_path):
    """Neueste Saison, in der schon gespielt wurde (Hauptrunde)."""
    today = datetime.date.today().isoformat()
    best = 0
    for g in rows(games_path):
        if g['game_type'] == 'REG' and g['gameday'] and g['gameday'] <= today:
            best = max(best, int(g['season']))
    return best


def pct_rank(values, v):
    """Perzentil 0..100 eines Werts innerhalb einer Liste."""
    if not values:
        return None
    below = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return round(100 * (below + 0.5 * equal) / len(values))


def r1(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, 1)


def r3(x):
    return None if x is None else round(x, 3)


def name_key(n):
    """Name vereinheitlichen, wie es auch die App tut: klein, ohne Satzzeichen und Namenszusätze."""
    n = re.sub(r"[^a-z ]", "", (n or "").lower().replace("-", " "))
    return " ".join(w for w in n.split() if w not in ("jr", "sr", "ii", "iii", "iv", "v"))


def build_history(Y, cache, out, meta_by_gsis, pfr2gsis, force=False):
    """Saisonbilanz je Spieler, Grundlage für die Draft-Planung. Abgeschlossene Saisons werden nur einmal gebaut,
    die laufende (force=True) jede Nacht neu."""
    path_out = os.path.join(out, f'history_{Y}.json')
    if os.path.exists(path_out) and not force:
        print(f'history_{Y}.json existiert, Saison abgeschlossen: übersprungen')
        return
    stats_p = fetch(f'stats_player/stats_player_week_{Y}.csv', cache)
    try:
        snaps_p = fetch(f'snap_counts/snap_counts_{Y}.csv', cache)
    except Exception:
        snaps_p = None
    agg, weekly = {}, defaultdict(list)
    for r in rows(stats_p):
        if r['season_type'] != 'REG':
            continue
        pos = r['position'] if r['position'] in POS else ('RB' if r['position'] == 'FB' else None)
        if not pos:
            continue
        g = r['player_id']
        a_ = agg.setdefault(g, {'n': r['player_display_name'], 'p': pos, 't': team(r['team']), 'g': 0, 'std': 0.0, 'ppr': 0.0, 'rec': 0.0, 'tgt': 0.0,
                               'airYds': 0.0, 'recYds': 0.0, 'car': 0.0, 'rushYds': 0.0, 'td': 0.0, 'att': 0.0, 'passYds': 0.0, 'passTd': 0.0,
                               'int': 0.0, 'tgtSh': [], 'airSh': [], 'snap': []})
        a_['t'] = team(r['team'])
        played = any(num(r[k]) for k in ('attempts', 'carries', 'targets', 'receptions')) or num(r['fantasy_points']) != 0
        if played:
            a_['g'] += 1
            a_['tgtSh'].append(num(r['target_share'])); a_['airSh'].append(num(r['air_yards_share']))
        std, rec = num(r['fantasy_points']), num(r['receptions'])
        a_['std'] += std; a_['ppr'] += num(r['fantasy_points_ppr']); a_['rec'] += rec
        a_['tgt'] += num(r['targets']); a_['airYds'] += num(r['receiving_air_yards']); a_['recYds'] += num(r['receiving_yards'])
        a_['car'] += num(r['carries']); a_['rushYds'] += num(r['rushing_yards'])
        a_['td'] += num(r['rushing_tds']) + num(r['receiving_tds']); a_['att'] += num(r['attempts'])
        a_['passYds'] += num(r['passing_yards']); a_['passTd'] += num(r['passing_tds']); a_['int'] += num(r['passing_interceptions'])
        if played:
            weekly[g] += [int(r['week']), round(std, 1), int(rec)]
    if snaps_p:
        for r in rows(snaps_p):
            g = pfr2gsis.get(r['pfr_player_id'])
            if r['game_type'] == 'REG' and g in agg:
                agg[g]['snap'].append(num(r['offense_pct']))
    res = {}
    for g, a_ in agg.items():
        if a_['g'] == 0:
            continue
        m = meta_by_gsis.get(g, {})
        res[g] = {
            'n': a_['n'], 'p': a_['p'], 't': a_['t'], 'g': a_['g'],
            'std': r1(a_['std']), 'ppr': r1(a_['ppr']), 'rec': int(a_['rec']), 'tgt': int(a_['tgt']), 'car': int(a_['car']),
            'recYds': int(a_['recYds']), 'rushYds': int(a_['rushYds']), 'td': int(a_['td']),
            'att': int(a_['att']), 'passYds': int(a_['passYds']), 'passTd': int(a_['passTd']), 'int': int(a_['int']),
            'tgtSh': r3(sum(a_['tgtSh']) / len(a_['tgtSh'])) if a_['tgtSh'] else None,
            'airSh': r3(sum(a_['airSh']) / len(a_['airSh'])) if a_['airSh'] else None,
            'adot': r1(a_['airYds'] / a_['tgt']) if a_['tgt'] else None,
            'ypc': r1(a_['rushYds'] / a_['car']) if a_['car'] else None,
            'snap': r3(sum(a_['snap']) / len(a_['snap'])) if a_['snap'] else None,
            'w': weekly[g],
        }
        if m.get('birth'):
            try:
                b = datetime.date.fromisoformat(m['birth'])
                res[g]['age'] = round((datetime.date(Y, 9, 1) - b).days / 365.25, 1)
            except ValueError:
                pass
    # Positionsränge nach PPR und Standard
    for key in ('ppr', 'std'):
        for pos in POS:
            lst = sorted((g for g in res if res[g]['p'] == pos), key=lambda g: -(res[g][key] or 0))
            for i, g in enumerate(lst):
                res[g].setdefault('rk', {})[key] = i + 1
    with open(path_out, 'w', encoding='utf-8') as f:
        json.dump({'season': Y, 'schema': {'w': ['week', 'std', 'rec']}, 'players': res}, f, ensure_ascii=False, separators=(',', ':'))
    print(f'history_{Y}.json: {os.path.getsize(path_out) / 1024:.0f} KB, {len(res)} Spieler')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data')
    ap.add_argument('--cache', default=os.path.join(os.environ.get('RUNNER_TEMP', '/tmp'), 'nflverse-cache'))
    ap.add_argument('--season', type=int, default=0)
    a = ap.parse_args()

    games_p = fetch('schedules/games.csv', a.cache)
    S = a.season or pick_season(games_p)
    print('Saison', S)
    stats_p = fetch(f'stats_player/stats_player_week_{S}.csv', a.cache)
    pbp_p = fetch(f'pbp/play_by_play_{S}.csv.gz', a.cache)
    snaps_p = fetch(f'snap_counts/snap_counts_{S}.csv', a.cache)
    players_p = fetch('players/players.csv', a.cache)
    ngs = {k: fetch(f'nextgen_stats/ngs_{k}.csv.gz', a.cache) for k in ('receiving', 'rushing', 'passing')}

    # ---------- Spielplan und Bye-Wochen ----------
    games, played = [], defaultdict(set)
    for g in rows(games_p):
        if int(g['season']) != S or g['game_type'] != 'REG':
            continue
        w, aw, hm = int(g['week']), team(g['away_team']), team(g['home_team'])
        games.append([w, aw, hm, g['gameday'], g['gametime'],
                      int(g['away_score']) if g['away_score'] not in ('', 'NA') else None,
                      int(g['home_score']) if g['home_score'] not in ('', 'NA') else None])
        played[aw].add(w); played[hm].add(w)
    weeks_all = sorted({g[0] for g in games})
    byes = {t: [w for w in weeks_all if w not in ws][0] for t, ws in played.items() if any(w not in ws for w in weeks_all)}

    # ---------- Spieler-Stammdaten ----------
    pfr2gsis, meta, meta_full = {}, {}, {}
    for p in rows(players_p):
        if p['pfr_id']:
            pfr2gsis[p['pfr_id']] = p['gsis_id']
        meta[p['gsis_id']] = (p['display_name'], p['position'], team(p['latest_team']))
        meta_full[p['gsis_id']] = {'birth': p['birth_date'], 'draft': [p['draft_year'], p['draft_round'], p['draft_pick']], 'exp': p['years_of_experience']}

    # ---------- Wochenstatistik ----------
    W = defaultdict(dict)        # gsis -> woche -> werte
    info = {}
    allowed = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # (def, woche) -> pos -> [std, rec]
    produced = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))  # (off, woche) -> pos -> [std, rec]
    last_week = 0
    for r in rows(stats_p):
        if r['season_type'] != 'REG':
            continue
        pos = r['position'] if r['position'] in POS else ('RB' if r['position'] == 'FB' else None)
        if not pos:
            continue
        g, w = r['player_id'], int(r['week'])
        last_week = max(last_week, w)
        tm, opp = team(r['team']), team(r['opponent_team'])
        info[g] = (r['player_display_name'], pos, tm)
        std, rec = num(r['fantasy_points']), num(r['receptions'])
        allowed[(opp, w)][pos][0] += std; allowed[(opp, w)][pos][1] += rec
        produced[(tm, w)][pos][0] += std; produced[(tm, w)][pos][1] += rec
        W[g][w] = {
            'opp': opp, 'tgt': num(r['targets']), 'tgtSh': num(r['target_share']), 'airSh': num(r['air_yards_share']),
            'rec': rec, 'recYds': num(r['receiving_yards']), 'airYds': num(r['receiving_air_yards']),
            'yac': num(r['receiving_yards_after_catch']), 'recTd': num(r['receiving_tds']),
            'car': num(r['carries']), 'rushYds': num(r['rushing_yards']), 'rushTd': num(r['rushing_tds']),
            'att': num(r['attempts']), 'cmp': num(r['completions']), 'passYds': num(r['passing_yards']),
            'passTd': num(r['passing_tds']), 'int': num(r['passing_interceptions']), 'cpoe': num(r['passing_cpoe'], None),
        }

    # ---------- Snap-Anteile ----------
    for r in rows(snaps_p):
        if r['game_type'] != 'REG':
            continue
        g = pfr2gsis.get(r['pfr_player_id'])
        if not g or g not in W:
            continue
        w = int(r['week'])
        d = W[g].setdefault(w, {'opp': team(r['opponent'])})
        d['snap'] = num(r['offense_pct'])

    # ---------- Play-by-Play: Ziele, Läufe, Red Zone, Defensivprofile ----------
    T = defaultdict(list)       # gsis -> flache Liste [w, ay, loc, res, rz] je Target
    C = defaultdict(list)       # gsis -> flache Liste [w, dir, yds, td, rz] je Lauf
    rz = defaultdict(lambda: defaultdict(lambda: [0, 0]))   # gsis -> woche -> [rzTgt, rzCar]
    team_car = defaultdict(lambda: defaultdict(float))       # team -> woche -> Läufe der RBs
    D = defaultdict(lambda: defaultdict(float))              # verteidigung -> kennzahlen
    LOC = {'left': 0, 'middle': 1, 'right': 2}
    for p in rows(pbp_p):
        if p['season_type'] != 'REG' or p['two_point_attempt'] == '1':
            continue
        pt = p['play_type']
        if pt not in ('pass', 'run'):
            continue
        w, de = int(p['week']), team(p['defteam'])
        yl, epa, yds = num(p['yardline_100'], 99), num(p['epa']), num(p['yards_gained'])
        inrz = 1 if yl <= 20 else 0
        if pt == 'pass' and p['qb_scramble'] != '1':
            D[de]['dropbacks'] += 1; D[de]['passEpa'] += epa; D[de]['sacks'] += num(p['sack'])
            D[de]['pressure'] += 1 if (p['sack'] == '1' or p['qb_hit'] == '1') else 0
            if p['receiver_player_id'] and p['sack'] != '1':
                ay = num(p['air_yards'], None)
                g = p['receiver_player_id']
                res = 3 if p['interception'] == '1' else (2 if p['pass_touchdown'] == '1' else (1 if p['complete_pass'] == '1' else 0))
                if ay is not None:
                    T[g] += [w, int(round(ay)), LOC.get(p['pass_location'], 9), res, inrz]
                    band = 'deep' if ay >= 20 else ('short' if ay < 10 else 'mid')
                    D[de][band + 'Att'] += 1; D[de][band + 'Yds'] += yds; D[de][band + 'Cmp'] += 1 if res in (1, 2) else 0
                if inrz:
                    rz[g][w][0] += 1
        elif pt == 'run' and p['qb_kneel'] != '1':
            D[de]['runs'] += 1; D[de]['runEpa'] += epa; D[de]['runYds'] += yds; D[de]['stuffs'] += 1 if yds <= 0 else 0
            g = p['rusher_player_id']
            if g:
                C[g] += [w, LOC.get(p['run_location'], 1), int(round(yds)), 1 if p['rush_touchdown'] == '1' else 0, inrz]
                if inrz:
                    rz[g][w][1] += 1
                if info.get(g, (0, ''))[1] == 'RB':
                    team_car[team(p['posteam'])][w] += 1

    # ---------- Next Gen Stats (Saisonwerte) ----------
    NG = defaultdict(dict)
    keys = {
        'receiving': {'sep': 'avg_separation', 'cush': 'avg_cushion', 'iay': 'avg_intended_air_yards', 'iaySh': 'percent_share_of_intended_air_yards', 'yacoe': 'avg_yac_above_expectation'},
        'rushing': {'eff': 'efficiency', 'box8': 'percent_attempts_gte_eight_defenders', 'ttl': 'avg_time_to_los', 'ryoe': 'rush_yards_over_expected_per_att'},
        'passing': {'ttt': 'avg_time_to_throw', 'aggr': 'aggressiveness', 'cpoe': 'completion_percentage_above_expectation', 'iay': 'avg_intended_air_yards'},
    }
    for k, path in ngs.items():
        for r in rows(path):
            if int(r['season']) == S and r['season_type'] == 'REG' and r['week'] == '0':
                for out, col in keys[k].items():
                    v = num(r.get(col), None)
                    if v is not None:
                        NG[r['player_gsis_id']][out] = round(v, 2)

    # ---------- Spielerprofile ----------
    order = ['opp', 'snap', 'tgt', 'tgtSh', 'airSh', 'rec', 'recYds', 'airYds', 'yac', 'recTd', 'car', 'rushYds', 'rushTd', 'rzTgt', 'rzCar', 'att', 'cmp', 'passYds', 'passTd', 'int']
    out_players = {}
    agg = {}
    for g, wk in W.items():
        name, pos, tm = info.get(g) or meta.get(g, ('?', '?', ''))
        if pos not in POS:
            continue
        rows_w, s = [], defaultdict(float)
        snaps = []
        for w in sorted(wk):
            d = dict(wk[w]); d['rzTgt'], d['rzCar'] = rz[g][w] if g in rz and w in rz[g] else (0, 0)
            if 'snap' in d:
                snaps.append(d['snap'])
            for k in order[2:]:
                s[k] += d.get(k, 0) or 0
            arr = [w] + [d.get('opp', '')] + [r3(d['snap']) if 'snap' in d else None] + [r3(d.get(k)) if k in ('tgtSh', 'airSh') else (d.get(k) or 0) for k in order[2:]]
            while arr and arr[-1] in (0, None, 0.0):
                arr.pop()
            rows_w.append(arr)
        games_n = sum(1 for w in wk if (wk[w].get('snap') or 0) > 0 or any((wk[w].get(k) or 0) for k in ('tgt', 'car', 'att')))
        if games_n == 0:
            continue
        sh_w = [wk[w].get('tgtSh') or 0 for w in wk if (wk[w].get('snap') or 0) > 0 or wk[w].get('tgt')]
        air_w = [wk[w].get('airSh') or 0 for w in wk if (wk[w].get('snap') or 0) > 0 or wk[w].get('tgt')]
        tcar = sum(team_car[tm].values()) if tm in team_car else 0
        m = {
            'g': games_n, 'snap': r3(sum(snaps) / len(snaps)) if snaps else None,
            'tgt': s['tgt'], 'tgtPg': r1(s['tgt'] / games_n), 'tgtSh': r3(sum(sh_w) / len(sh_w)) if sh_w else None,
            'airSh': r3(sum(air_w) / len(air_w)) if air_w else None,
            'adot': r1(s['airYds'] / s['tgt']) if s['tgt'] else None, 'catch': r3(s['rec'] / s['tgt']) if s['tgt'] else None,
            'yacR': r1(s['yac'] / s['rec']) if s['rec'] else None, 'rec': s['rec'], 'recYds': s['recYds'], 'recTd': s['recTd'],
            'rzTgt': s['rzTgt'], 'car': s['car'], 'carPg': r1(s['car'] / games_n), 'ypc': r1(s['rushYds'] / s['car']) if s['car'] else None,
            'rushYds': s['rushYds'], 'rushTd': s['rushTd'], 'rzCar': s['rzCar'],
            'carSh': r3(s['car'] / tcar) if (pos == 'RB' and tcar) else None,
            'att': s['att'], 'passYds': s['passYds'], 'passTd': s['passTd'], 'int': s['int'],
            'ypa': r1(s['passYds'] / s['att']) if s['att'] else None, 'rushPg': r1(s['rushYds'] / games_n),
        }
        agg[g] = (pos, m)
        out_players[g] = {'n': name, 'p': pos, 't': tm, 'w': rows_w, 's': m}
        if g in NG:
            out_players[g]['ngs'] = NG[g]

    # ---------- Perzentile innerhalb der Position und Spielertyp ----------
    qual = {'QB': lambda m: m['att'] >= 80, 'RB': lambda m: m['car'] >= 25 or m['tgt'] >= 15,
            'WR': lambda m: m['tgt'] >= 15, 'TE': lambda m: m['tgt'] >= 12}
    metrics = {'QB': ['ypa', 'rushPg', 'passTd'], 'RB': ['carPg', 'ypc', 'tgtSh', 'carSh', 'rzCar', 'snap'],
               'WR': ['tgtSh', 'airSh', 'adot', 'yacR', 'catch', 'rzTgt', 'snap'], 'TE': ['tgtSh', 'airSh', 'adot', 'yacR', 'catch', 'rzTgt', 'snap']}
    pools = defaultdict(lambda: defaultdict(list))
    for g, (pos, m) in agg.items():
        if qual[pos](m):
            for k in metrics[pos]:
                if m.get(k) is not None:
                    pools[pos][k].append(m[k])
    for g, (pos, m) in agg.items():
        if not qual[pos](m):
            continue
        pc = {k: pct_rank(pools[pos][k], m[k]) for k in metrics[pos] if m.get(k) is not None}
        out_players[g]['pc'] = pc
        ty = []
        ng = NG.get(g, {})
        if pos in ('WR', 'TE'):
            if (m['tgtSh'] or 0) >= .24: ty.append('Volumen-Ziel')
            if (m['adot'] or 0) >= 13: ty.append('Deep Threat')
            elif (m['adot'] or 99) <= 7 and (m['yacR'] or 0) >= 5.5: ty.append('YAC-Spieler')
            elif (m['adot'] or 99) <= 10 and (m['catch'] or 0) >= .7: ty.append('Possession')
            if pc.get('rzTgt', 0) >= 85: ty.append('Red-Zone-Ziel')
            if (ng.get('sep') or 0) >= 3.6: ty.append('Freiläufer')
        elif pos == 'RB':
            if (m['carSh'] or 0) >= .6: ty.append('Workhorse')
            elif (m['carSh'] or 0) >= .35: ty.append('Teil einer Rotation')
            else: ty.append('Ergänzung')
            if (m['tgtSh'] or 0) >= .1: ty.append('Passempfänger')
            if pc.get('rzCar', 0) >= 85: ty.append('Goal-Line-Back')
        elif pos == 'QB':
            if (m['rushPg'] or 0) >= 30: ty.append('Dual Threat')
            if (ng.get('aggr') or 0) >= 18 or (ng.get('iay') or 0) >= 8.5: ty.append('Gunslinger')
            if not ty: ty.append('Pocket Passer')
        out_players[g]['ty'] = ty[:3]

    # ---------- Defensivprofile: zugelassene Punkte, gegnerbereinigt ----------
    off_avg = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    for (tm, w), pp in produced.items():
        for pos, (std, rec) in pp.items():
            a_ = off_avg[tm][pos]; a_[0] += std; a_[1] += rec; a_[2] += 1
    opp_of = {}
    for g_ in games:
        opp_of[(g_[1], g_[0])] = g_[2]; opp_of[(g_[2], g_[0])] = g_[1]
    def_rows = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0]))   # def -> pos -> [std, rec, stdAdj, recAdj, n]
    for (de, w), pp in allowed.items():
        off = opp_of.get((de, w))
        for pos in POS:
            std, rec = pp[pos] if pos in pp else (0.0, 0.0)
            oa = off_avg[off][pos] if off else [0, 0, 0]
            e_std, e_rec = (oa[0] / oa[2], oa[1] / oa[2]) if oa[2] else (std, rec)
            x = def_rows[de][pos]; x[0] += std; x[1] += rec; x[2] += std - e_std; x[3] += rec - e_rec; x[4] += 1
    defense = {}
    for de, pp in def_rows.items():
        d = D[de]
        prof = {'vs': {}}
        for pos, (std, rec, sa, ra, n) in pp.items():
            prof['vs'][pos] = {'std': r1(std / n), 'rec': r1(rec / n), 'stdAdj': r1(sa / n), 'recAdj': r1(ra / n), 'g': n}
        def rate(a_, b_):
            return r3(d[a_] / d[b_]) if d[b_] else None
        prof['pass'] = {'epa': rate('passEpa', 'dropbacks'), 'sack': rate('sacks', 'dropbacks'), 'press': rate('pressure', 'dropbacks'),
                        'shortYpa': r1(d['shortYds'] / d['shortAtt']) if d['shortAtt'] else None,
                        'deepYpa': r1(d['deepYds'] / d['deepAtt']) if d['deepAtt'] else None,
                        'deepCmp': rate('deepCmp', 'deepAtt')}
        prof['run'] = {'epa': rate('runEpa', 'runs'), 'ypc': r1(d['runYds'] / d['runs']) if d['runs'] else None, 'stuff': rate('stuffs', 'runs')}
        defense[de] = prof

    # Ränge: 1 = schwerste Defense (lässt am wenigsten zu)
    def rank(getter, low_is_tough=True):
        vals = [(de, getter(p)) for de, p in defense.items() if getter(p) is not None]
        vals.sort(key=lambda x: x[1] if low_is_tough else -x[1])
        return {de: i + 1 for i, (de, _) in enumerate(vals)}
    for pos in POS:
        rk = rank(lambda p, pos=pos: p['vs'].get(pos, {}).get('stdAdj'))
        for de, r_ in rk.items():
            defense[de]['vs'][pos]['rank'] = r_
    for key, path, low in (('passEpa', ('pass', 'epa'), True), ('runEpa', ('run', 'epa'), True), ('deep', ('pass', 'deepYpa'), True), ('press', ('pass', 'press'), False)):
        rk = rank(lambda p, path=path: p[path[0]].get(path[1]), low)
        for de, r_ in rk.items():
            defense[de].setdefault('rank', {})[key] = r_

    # ---------- Schreiben ----------
    os.makedirs(a.out, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

    def dump(name, obj):
        p = os.path.join(a.out, name)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
        print(f'{name}: {os.path.getsize(p) / 1024:.0f} KB')

    dump('players.json', {'schema': ['week'] + order, 'players': out_players})
    dump('targets.json', {'schema': {'t': ['week', 'airYards', 'loc', 'res', 'rz'], 'c': ['week', 'dir', 'yds', 'td', 'rz']},
                          't': {g: v for g, v in T.items() if g in out_players}, 'c': {g: v for g, v in C.items() if g in out_players}})
    dump('defense.json', {'teams': defense})
    dump('schedule.json', {'games': games, 'byes': byes, 'tz': 'America/New_York'})
    # Zuordnung Sleeper -> NFL: Sleeper liefert die GSIS-ID nicht für alle Spieler,
    # deshalb zusätzlich über die ESPN-ID und über Name plus Position
    ids_espn, ids_name, seen = {}, {}, set()
    for p in rows(players_p):
        g, pos = p['gsis_id'], p['position']
        if not g or pos not in POS or not (p['last_season'] or '').isdigit() or int(p['last_season']) < S - 3:
            continue
        if p['espn_id']:
            ids_espn[p['espn_id'].split('.')[0]] = g
        k = name_key(p['display_name']) + '|' + pos
        if k in seen:
            ids_name.pop(k, None)   # mehrdeutig: lieber keine Zuordnung
        else:
            seen.add(k); ids_name[k] = g
    dump('ids.json', {'espn': ids_espn, 'name': ids_name})
    # Vorsaisons für die Draft-Planung (abgeschlossene Saisons werden nur einmal gebaut)
    hist = []
    for Y in range(S - 3, S + 1):
        try:
            build_history(Y, a.cache, a.out, meta_full, pfr2gsis, force=(Y == S))
            hist.append(f'history_{Y}.json')
        except Exception as e:
            print(f'Vorsaison {Y} übersprungen: {e}', file=sys.stderr)
    dump('meta.json', {'season': S, 'lastWeek': last_week, 'generated': now, 'players': len(out_players), 'history': hist,
                       'source': 'nflverse (github.com/nflverse/nflverse-data), CC BY 4.0',
                       'files': ['players.json', 'targets.json', 'defense.json', 'schedule.json', 'ids.json']})


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('Fehler:', e, file=sys.stderr)
        sys.exit(1)
