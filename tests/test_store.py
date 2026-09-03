from datetime import date, timedelta

from models import Event, Stage
from store import State, Store, event_hash, normalise

TODAY = date(2026, 8, 13)


def make_store(tmp_path):
    state = State(tmp_path / "state.json")
    return Store(tmp_path / "seen.jsonl", tmp_path / "events.db", state), state


def ev(title="Porto Robotics Hackathon 2026", org="JuniFEUP", start=date(2026, 11, 7), **kw):
    return Event(title=title, organiser=org, start_date=start,
                 url="https://example.com/e", **kw)


def test_normalise_and_hash_stable():
    assert normalise("Estágio — Mecânica!") == "estagio mecanica"
    a = event_hash("Porto Hack", "JuniFEUP", date(2026, 11, 7))
    b = event_hash("porto  HACK", "junifeup", date(2026, 11, 7))
    assert a == b


def test_exact_duplicate(tmp_path):
    store, _ = make_store(tmp_path)
    s1, e1 = store.upsert(ev(), TODAY)
    s2, e2 = store.upsert(ev(), TODAY)
    assert s1 == "new" and e1.id == "E-0001"
    assert s2 == "duplicate" and e2.id == "E-0001"


def test_fuzzy_reworded_repost(tmp_path):
    store, _ = make_store(tmp_path)
    store.upsert(ev(), TODAY)
    s, _ = store.upsert(ev(title="Porto Robotics  Hackathon 2026!!!"), TODAY)
    assert s == "duplicate"                      # pure rewording, same facts
    s2, _ = store.upsert(ev(title="Porto Robotics Hackathon 2026 v2",
                            start=date(2026, 11, 8)), TODAY)
    assert s2.startswith("resurfaced")           # fuzzy hit + date changed


def test_material_change_resurfaces(tmp_path):
    store, _ = make_store(tmp_path)
    store.upsert(ev(next_deadline=date(2026, 10, 1)), TODAY)
    s, merged = store.upsert(ev(next_deadline=date(2026, 10, 15)), TODAY)
    assert s.startswith("resurfaced") and "deadline" in s
    assert merged.next_deadline == date(2026, 10, 15)


def test_dismissed_stays_suppressed_even_with_stage(tmp_path):
    store, _ = make_store(tmp_path)
    _, e = store.upsert(ev(stages=[Stage(name="Round 1", closes=TODAY + timedelta(days=5))]), TODAY)
    store.dismiss(e, "not_interested", TODAY)
    s, _ = store.upsert(ev(), TODAY)
    assert s == "suppressed"
    assert store.stage_alerts(TODAY) == []  # user dismissal beats resurfacing


def test_stage_alerts_fire_once(tmp_path):
    store, _ = make_store(tmp_path)
    store.upsert(ev(stages=[Stage(name="Early bird", opens=TODAY - timedelta(days=1),
                                  closes=TODAY + timedelta(days=6),
                                  is_advantageous=True)]), TODAY)
    kinds = sorted(k for _, _, k in store.stage_alerts(TODAY))
    assert kinds == ["opens", "t7"]
    assert store.stage_alerts(TODAY) == []          # once each
    later = store.stage_alerts(TODAY + timedelta(days=5))
    assert [k for _, _, k in later] == ["t2"]


def test_persistence_roundtrip_and_sqlite_ephemeral(tmp_path):
    store, state = make_store(tmp_path)
    store.upsert(ev(), TODAY)
    store.save(); state.save()
    store.close()                                   # release the SQLite handle first
    (tmp_path / "events.db").unlink()               # losing the db loses nothing
    store2, _ = make_store(tmp_path)
    assert len(store2.open) == 1
    only = next(iter(store2.open.values()))
    assert only.id == "E-0001" and only.title.startswith("Porto Robotics")


def test_prune_after_60_days(tmp_path):
    store, _ = make_store(tmp_path)
    store.upsert(ev(next_deadline=TODAY - timedelta(days=61)), TODAY)
    store.upsert(ev(title="Fresh comp", next_deadline=TODAY + timedelta(days=5)), TODAY)
    assert store.prune(TODAY) == 1
    assert len(store.open) == 1
