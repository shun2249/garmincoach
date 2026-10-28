"""python-garminconnect 的薄包裝：登入 / token 快取 / 安全呼叫 / 批次抓取。"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from . import config

log = logging.getLogger(__name__)


class GarminClient:
    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        token_dir: Optional[Path] = None,
        is_cn: Optional[bool] = None,
        prompt_mfa: Optional[Callable[[], str]] = None,
    ) -> None:
        self.email = email or config.GARMIN_EMAIL
        self.password = password or config.GARMIN_PASSWORD
        self.token_dir = Path(token_dir or config.TOKEN_DIR).expanduser()
        self.is_cn = config.GARMIN_IS_CN if is_cn is None else is_cn
        self.prompt_mfa = prompt_mfa or (lambda: input("Garmin MFA 驗證碼: ").strip())
        self.api: Optional[Garmin] = None

    # ---------- 登入 ----------
    def login(self) -> Garmin:
        """優先用已存的 token，失敗才用帳密（會觸發 MFA 提示）。"""
        self.token_dir.mkdir(parents=True, exist_ok=True)
        api = Garmin(is_cn=self.is_cn)
        try:
            api.login(str(self.token_dir))
            log.info("使用快取 token 登入成功 (%s)", api.display_name)
        except (FileNotFoundError, GarminConnectAuthenticationError, GarminConnectConnectionError, Exception) as e:
            log.info("token 登入失敗 (%s)，改用帳號密碼", type(e).__name__)
            if not self.email or not self.password:
                raise GarminConnectAuthenticationError(
                    "沒有可用的 token，且 .env 未設定 GARMIN_EMAIL / GARMIN_PASSWORD"
                ) from e
            api = Garmin(self.email, self.password, is_cn=self.is_cn, prompt_mfa=self.prompt_mfa)
            api.login(str(self.token_dir))
            api.client.dump(str(self.token_dir))
            log.info("帳密登入成功，token 已存到 %s", self.token_dir)
        self.api = api
        return api

    def _require(self) -> Garmin:
        if self.api is None:
            self.login()
        assert self.api is not None
        return self.api

    # ---------- 安全呼叫 ----------
    def safe(self, name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
        """呼叫 Garmin API 方法，單一 endpoint 失敗不會中斷整批同步。"""
        api = self._require()
        fn = getattr(api, name)
        try:
            result = fn(*args, **kwargs)
        except GarminConnectTooManyRequestsError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("%s%s 失敗: %s", name, args, e)
            result = default
        time.sleep(config.API_PAUSE_SECONDS)
        return result

    # ---------- 批次抓取 ----------
    def fetch_daily(self, day: date) -> dict[str, Any]:
        """抓一天的生理恢復資料，回傳 {endpoint 名稱: 原始 JSON}。"""
        d = day.isoformat()
        return {
            "user_summary": self.safe("get_user_summary", d),
            "sleep": self.safe("get_sleep_data", d),
            "hrv": self.safe("get_hrv_data", d),
            "rhr": self.safe("get_rhr_day", d),
            "stress": self.safe("get_stress_data", d),
            "training_readiness": self.safe("get_training_readiness", d),
            "training_status": self.safe("get_training_status", d),
            "max_metrics": self.safe("get_max_metrics", d),
        }

    def fetch_activity_list(self, start: date, end: date) -> list[dict[str, Any]]:
        acts = self.safe("get_activities_by_date", start.isoformat(), end.isoformat(), default=[])
        return acts or []

    def fetch_recent_activities(self, limit: int = 20) -> list[dict[str, Any]]:
        acts = self.safe("get_activities", 0, limit, default=[])
        return acts or []

    def fetch_activity_bundle(self, activity_id: int, with_details: bool = True) -> dict[str, Any]:
        """一筆活動的完整資料：摘要、分段、天氣、區間、逐秒資料（用來算 HR drift）。"""
        aid = str(activity_id)
        bundle: dict[str, Any] = {
            "activity": self.safe("get_activity", aid),
            "splits": self.safe("get_activity_splits", aid),
            "weather": self.safe("get_activity_weather", aid),
            "hr_zones": self.safe("get_activity_hr_in_timezones", aid),
            "power_zones": self.safe("get_activity_power_in_timezones", aid),
        }
        if with_details:
            bundle["details"] = self.safe("get_activity_details", aid, 2000, 0)
        return bundle

    def fetch_physio(self, day: date) -> dict[str, Any]:
        d = day.isoformat()
        return {
            "lactate_threshold": self.safe("get_lactate_threshold", latest=True),
            "cycling_ftp": self.safe("get_cycling_ftp"),
            "race_predictions": self.safe("get_race_predictions"),
            "endurance_score": self.safe("get_endurance_score", d),
            "hill_score": self.safe("get_hill_score", d),
        }

    def fetch_profile(self) -> dict[str, Any]:
        """身體資料、心率/功率區間設定、最近體重。"""
        api = self._require()
        today = date.today()

        def raw(path: str) -> Any:
            try:
                r = api.connectapi(path)
            except Exception as e:  # noqa: BLE001
                log.warning("%s 失敗: %s", path, e)
                r = None
            time.sleep(config.API_PAUSE_SECONDS)
            return r

        return {
            "profile": self.safe("get_user_profile"),
            "settings": self.safe("get_userprofile_settings"),
            "hr_zones": raw("/biometric-service/heartRateZones/"),
            "power_zones": raw("/biometric-service/powerZones/sports/all"),
            "weigh_ins": self.safe("get_weigh_ins", (today - timedelta(days=90)).isoformat(), today.isoformat()),
            "devices": self.safe("get_devices"),
        }

    def fetch_history(self, start: date, end: date) -> dict[str, Any]:
        """一年內體能指標的歷史序列（週/月）。"""
        api = self._require()
        s, e = start.isoformat(), end.isoformat()
        try:
            maxmet = api.connectapi(f"/metrics-service/metrics/maxmet/daily/{s}/{e}")
        except Exception as ex:  # noqa: BLE001
            log.warning("maxmet range 失敗: %s", ex)
            maxmet = None
        time.sleep(config.API_PAUSE_SECONDS)
        return {
            "race_predictions": self.safe("get_race_predictions", s, e, "monthly"),
            "lactate_threshold": self.safe("get_lactate_threshold", latest=False, start_date=s, end_date=e, aggregation="weekly"),
            "vo2max": maxmet,
            "endurance": self.safe("get_endurance_score", s, e),
            "hill": self.safe("get_hill_score", s, e),
            "running_tolerance": self.safe("get_running_tolerance", s, e),
        }


def dump_raw(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
