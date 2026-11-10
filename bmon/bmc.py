import json
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import requests
import urllib3


@dataclass
class SensorReading:
    source: str
    name: str
    value: float | None
    unit: str
    grp: str
    status: str
    thresholds: dict | None = None


class BmcError(Exception):
    pass


_GROUP_BY_UNIT = {"degrees C": "temp", "Volts": "volt", "RPM": "fan",
                  "Watts": "power"}


def parse_threshold_sensors(doc: dict) -> list[SensorReading]:
    out = []
    for s in doc.get("Sensors", []):
        raw_value = s.get("ReadingValue")
        out.append(SensorReading(
            source="bmc", name=s["Name"],
            value=float(raw_value) if raw_value is not None else None,
            unit=s.get("Unit", ""),
            grp=_GROUP_BY_UNIT.get(s.get("Unit", ""), "other"),
            status=str(s.get("Status") or "ns"),
            thresholds={
                "upper_nc": s.get("UpperThresholdNonCritical"),
                "upper_crit": s.get("UpperThresholdCritical"),
                "upper_fatal": s.get("UpperThresholdFatal"),
                "lower_nc": s.get("LowerThresholdNonCritical"),
                "lower_crit": s.get("LowerThresholdCritical"),
                "lower_fatal": s.get("LowerThresholdFatal")}))
    return out


def parse_discrete_sensors(doc: dict) -> list[SensorReading]:
    out = []
    for s in doc.get("Sensors", []):
        raw_status = s.get("Status")
        if raw_status is None:
            status, value = "ns", None
        else:
            status = str(raw_status)
            try:
                value = (float(int(status, 16))
                         if status.lower().startswith("0x") else 0.0)
            except ValueError:
                value = 0.0
        out.append(SensorReading(source="bmc", name=s["Name"], value=value,
                                 unit="status", grp="status", status=status))
    return out


def parse_power(doc: dict) -> list[SensorReading]:
    out = []
    pc = (doc.get("PowerControl") or [{}])[0]
    if pc.get("PowerConsumedWatts") is not None:
        out.append(SensorReading(
            source="psu", name="Total Power",
            value=float(pc["PowerConsumedWatts"]), unit="Watts",
            grp="power", status="ok"))
    for ps in doc.get("PowerSupplies") or []:
        if (ps.get("Status") or {}).get("State") != "Enabled":
            continue  # PSU1 是 Absent
        nm = ps.get("Name", "PSU")
        oem = (ps.get("Oem") or {}).get("Public") or {}
        fields = [("Input V", ps.get("LineInputVoltage"), "Volts"),
                  ("Output W", ps.get("PowerOutputWatts") if ps.get(
                      "PowerOutputWatts") is not None else oem.get(
                          "PowerOutputWatts"), "Watts"),
                  ("Input W", oem.get("PowerInputWatts"), "Watts"),
                  ("Output A", oem.get("OutputAmperage"), "Amps"),
                  ("Output V", oem.get("OutputVoltage"), "Volts")]
        for label, v, unit in fields:
            if v is not None:
                out.append(SensorReading(
                    source="psu", name=f"{nm} {label}", value=float(v),
                    unit=unit, grp="psu", status="ok"))
    return out


def parse_sel_entry(doc: dict) -> dict:
    ts_str = doc.get("EventTimestamp") or doc.get("Created") or ""
    ts = int(datetime.fromisoformat(ts_str).timestamp()) if ts_str else 0
    return {"id": int(doc["Id"]), "ts": ts, "severity": doc.get("Severity", ""),
            "event_type": doc.get("EventType", ""), "message": doc.get("Message", ""),
            "message_id": doc.get("MessageId", ""),
            "raw": json.dumps(doc, ensure_ascii=False)}


class BmcClient:
    def __init__(self, base: str, user: str, password: str, verify: bool = False):
        self.base = base.rstrip("/")
        self._s = requests.Session()
        self._s.auth = (user, password)
        self._s.verify = verify
        self._s.headers["Accept"] = "application/json"
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _get(self, path: str) -> dict:
        try:
            r = self._s.get(f"{self.base}{path}", timeout=8)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise BmcError(f"GET {path} 失败: {e}") from e

    def get_all_readings(self) -> list[SensorReading]:
        readings = []
        readings += parse_threshold_sensors(
            self._get("/redfish/v1/Chassis/1/ThresholdSensors"))
        readings += parse_discrete_sensors(
            self._get("/redfish/v1/Chassis/1/DiscreteSensors"))
        readings += parse_power(self._get("/redfish/v1/Chassis/1/Power"))
        return readings

    def get_sel_entries(self, since_id: int = 0) -> list[dict]:
        # 该 BMC 的 Entries 集合分页(每页 32 条,Members@odata.nextLink 翻页),
        # 必须收集全部页的 id,否则首次同步只能拿到第一页。
        ids = []
        path = "/redfish/v1/Systems/1/LogServices/Log1/Entries"
        for _ in range(64):  # 页数上限,防 nextLink 成环/异常固件
            doc = self._get(path)
            for m in doc.get("Members", []):
                tail = m.get("@odata.id", "").rstrip("/").rsplit("/", 1)[-1]
                if tail.isdigit():
                    ids.append(int(tail))
            nxt = doc.get("Members@odata.nextLink")
            if not nxt:
                break
            if nxt.startswith(("http://", "https://")):
                p = urlsplit(nxt)
                nxt = p.path + (f"?{p.query}" if p.query else "")
            path = nxt
        new = sorted((i for i in set(ids) if i > since_id),
                     reverse=True)[:200]  # 最新在前,首次同步限 200 条(去重防重复页)
        return [parse_sel_entry(
                    self._get(f"/redfish/v1/Systems/1/LogServices/Log1/Entries/{i}"))
                for i in new]
