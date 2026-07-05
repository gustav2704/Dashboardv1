from __future__ import annotations

import importlib.util
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sqx_background_oos.py"
SPEC = importlib.util.spec_from_file_location("sqx_background_oos", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


def source(symbol: str, instrument: str, usymbol: str) -> dict[str, object]:
    return {
        "CONNECTION": "History",
        "SYMBOL": symbol,
        "INSTRUMENT": instrument,
        "TIMEFRAME": "M1",
        "TIMEZONE": "Etc/UCT",
        "DATEFROM": 1451606400000,
        "DATETO": 1782864000000,
        "DATATYPE": 1,
        "ROWS": 3_200_000,
        "DECIMALS": 1,
        "SOURCE": 2,
        "USYMBOL": usymbol,
        "USYMBOLNAME": usymbol,
        "REMOVE_WEEKENDS": 0,
        "BROKER_ID": -1,
    }


def test_dax_resolution_never_uses_usa30() -> None:
    sources = [
        source("USA30IDXUSD_clonedwnx", "USA30IDXUSD", "USA30IDXUSD"),
        source("DEUIDXEUR_M1", "DEUIDXEUR", "DEUIDXEUR"),
    ]
    resolved = runner.resolve_history_source(
        "DEUIDXEUR_clonedwnx", "2016.04.21", "2026.04.17", sources
    )
    assert resolved["SYMBOL"] == "DEUIDXEUR_M1"
    assert runner.symbol_family(resolved["SYMBOL"]) == "DAX"


def test_dax_resolution_fails_with_only_usa30() -> None:
    sources = [source("USA30IDXUSD_clonedwnx", "USA30IDXUSD", "USA30IDXUSD")]
    try:
        runner.resolve_history_source(
            "DEUIDXEUR_clonedwnx", "2016.04.21", "2026.04.17", sources
        )
    except RuntimeError as exc:
        assert "familia DAX" in str(exc)
    else:
        raise AssertionError("USA30 must never satisfy a DAX history request")


def test_apply_history_preserves_economic_profile() -> None:
    data = ET.fromstring(
        """<Data><Setups><Setup><Chart symbol="DEUIDXEUR_clonedwnx"
        timeframe="H1" spread="1"/></Setup></Setups></Data>"""
    )
    resources = ET.fromstring(
        """<Resources><Symbols><Symbol name="DEUIDXEUR_clonedwnx"
        source="2" timezone="EET" cloneFrom="DEUIDXEUR">
        <InstrumentInfo instrument="DAX_DWNX" alias="DAX_DWNX"
        pointValue="10.0" commissions="commission-profile"
        swap="swap-profile"/></Symbol></Symbols></Resources>"""
    )
    runner.apply_history_source(
        data, resources, source("DEUIDXEUR_M1", "DEUIDXEUR", "DEUIDXEUR")
    )
    chart = data.find("./Setups/Setup/Chart")
    instrument = resources.find("./Symbols/Symbol/InstrumentInfo")
    assert chart is not None and chart.get("symbol") == "DEUIDXEUR_M1"
    assert chart.get("spread") == "1"
    assert instrument is not None and instrument.get("instrument") == "DEUIDXEUR"
    assert instrument.get("pointValue") == "10.0"
    assert instrument.get("commissions") == "commission-profile"
    assert instrument.get("swap") == "swap-profile"
    assert "alias" not in instrument.attrib


def test_load_history_sources_reads_only_visible_m1(tmp_path: Path) -> None:
    database = tmp_path / "data.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE DATA(
             CONNECTION TEXT,SYMBOL TEXT,INSTRUMENT TEXT,TIMEFRAME TEXT,
             TIMEZONE TEXT,DATEFROM INTEGER,DATETO INTEGER,DATATYPE INTEGER,
             ROWS INTEGER,DECIMALS INTEGER,SOURCE INTEGER,USYMBOL TEXT,
             USYMBOLNAME TEXT,REMOVE_WEEKENDS INTEGER,BROKER_ID INTEGER,
             SHOW INTEGER)"""
    )
    values = source("DEUIDXEUR_M1", "DEUIDXEUR", "DEUIDXEUR")
    connection.execute(
        """INSERT INTO DATA VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            values["CONNECTION"],
            values["SYMBOL"],
            values["INSTRUMENT"],
            values["TIMEFRAME"],
            values["TIMEZONE"],
            values["DATEFROM"],
            values["DATETO"],
            values["DATATYPE"],
            values["ROWS"],
            values["DECIMALS"],
            values["SOURCE"],
            values["USYMBOL"],
            values["USYMBOLNAME"],
            values["REMOVE_WEEKENDS"],
            values["BROKER_ID"],
            1,
        ),
    )
    connection.commit()
    connection.close()
    rows = runner.load_history_sources(database)
    assert [row["SYMBOL"] for row in rows] == ["DEUIDXEUR_M1"]
