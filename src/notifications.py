import requests
import logging
import threading
from typing import List, Dict
from src.branding import PRODUCT_NAME, PRODUCT_SLUG


logger = logging.getLogger(f"{PRODUCT_SLUG}.notifications")


def _brand_text(value: str) -> str:
    return value


def _brand_payload(value):
    if isinstance(value, str):
        return _brand_text(value)
    if isinstance(value, list):
        return [_brand_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _brand_payload(item) for key, item in value.items()}
    return value


def is_valid_apprise_url(url: str) -> bool:
    try:
        import apprise

        client = apprise.Apprise()
        return bool(client.add(url))
    except Exception:
        return False


def _post(webhook_url: str, payload: dict):
    if not webhook_url:
        return
    # Validate it's actually a Discord webhook URL to prevent SSRF
    if not webhook_url.startswith("https://discord.com/api/webhooks/") and \
       not webhook_url.startswith("https://discordapp.com/api/webhooks/"):
        logger.warning("Discord delivery skipped: invalid webhook URL")
        return
    try:
        response = requests.post(webhook_url, json=_brand_payload(payload), timeout=10)
        logger.info("Discord delivery completed with HTTP %s",
                    response.status_code)
    except Exception as exc:
        # HTTP client exception text can contain the credential-bearing URL.
        logger.warning("Discord delivery failed (%s)", type(exc).__name__)


def _check_fields(checks: Dict) -> list:
    return [
        {
            "name":   name,
            "value":  ("✅ " if c["pass"] else "❌ ") + c["detail"],
            "inline": False,
        }
        for name, c in checks.items()
    ]


def _build_tv_tree(items: List[Dict]) -> dict:
    tree: dict = {}
    for ep in (item for item in items if item.get("type") == "episode"):
        show   = ep.get("grandparent_title") or ep.get("parent_title") or "Unknown Show"
        s_num  = ep.get("parent_index", "")
        season = f"Season {s_num}" if s_num else (ep.get("parent_title") or "Unknown Season")
        ep_num = ep.get("index", "")
        label  = f"Ep {ep_num} \u2013 {ep['title']}" if ep_num else ep["title"]
        tree.setdefault(show, {}).setdefault(season, []).append((int(ep_num) if str(ep_num).isdigit() else 999, label))
    for show in tree:
        for season in tree[show]:
            tree[show][season].sort(key=lambda x: x[0])
            tree[show][season] = [label for _, label in tree[show][season]]
    for s in (item for item in items if item.get("type") == "season"):
        show   = s.get("parent_title") or s.get("grandparent_title") or "Unknown Show"
        s_num  = s.get("index", "") or s.get("parent_index", "")
        season = f"Season {s_num}" if s_num else s["title"]
        tree.setdefault(show, {}).setdefault(season, [])
    for sh in (item for item in items if item.get("type") == "show"):
        tree.setdefault(sh["title"], {})
    return tree


def _season_number(label: str) -> int:
    parts = label.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 999


def _format_tv_tree(items: List[Dict]) -> str:
    """Build a hierarchical show → season → episode listing for Discord."""
    tree = _build_tv_tree(items)
    lines = []
    for show_name in sorted(tree):
        lines.append(f"**{show_name}**")
        for season in sorted(tree[show_name], key=_season_number):
            lines.append(f"\u00a0\u00a0{season}")
            for ep in tree[show_name][season]:
                lines.append(f"\u00a0\u00a0\u00a0\u00a0\u2022 {ep}")
    return "\n".join(lines)


def _item_lines(items: List[Dict], limit: int, noun: str = "") -> List[str]:
    lines = []
    for item in items[:limit]:
        year = f" ({item['year']})" if item.get("year") else ""
        lines.append(f"• {item['title']}{year}")
    if len(items) > limit:
        suffix = f" {noun}" if noun else ""
        lines.append(f"_…and {len(items) - limit} more{suffix}_")
    return lines


def _removed_item_lines(items: List[Dict]) -> List[str]:
    tv_items = [
        item for item in items
        if item.get("type") in ("episode", "season", "show")
    ]
    movies = [item for item in items if item.get("type") == "movie"]
    if not tv_items and not movies:
        return _item_lines(items, 15)
    lines = [_format_tv_tree(tv_items)] if tv_items else []
    if movies:
        if lines:
            lines.append("")
        lines.extend(_item_lines(movies, 20, "movies"))
    return lines


def _append_embed_body(description: str, lines: List[str]) -> str:
    if not lines:
        return description
    body = "\n".join(lines)
    if len(description) + len(body) + 2 > 4000:
        body = body[:4000 - len(description) - 20] + "\n_…(truncated)_"
    return f"{description}\n\n{body}"


def notify_emptied(webhook_url: str, instance_name: str, library_name: str,
                   removed_items: List[Dict], checks: Dict, breakdown: str = ""):
    """Fired when trash was actually emptied (items removed)."""
    if not webhook_url:
        return

    count       = len(removed_items)
    description = f"Emptied **{breakdown or f'{count} item(s)'}** from trash."

    description = _append_embed_body(
        description, _removed_item_lines(removed_items),
    )

    _post(webhook_url, {"embeds": [{
        "title":       f"✅ {PRODUCT_NAME} — {instance_name} / {library_name}",
        "description": description,
        "color":       0x3ecf8e,
        "fields":      _check_fields(checks),
    }]})


def notify_clean(webhook_url: str, instance_name: str, library_name: str,
                 checks: Dict):
    """Fired when run succeeded but trash was already empty."""
    if not webhook_url:
        return
    _post(webhook_url, {"embeds": [{
        "title":       f"✅ {PRODUCT_NAME} — {instance_name} / {library_name}",
        "description": "Trash was already empty — nothing to remove.",
        "color":       0x3ecf8e,
        "fields":      _check_fields(checks),
    }]})


def notify_health_fail(webhook_url: str, instance_name: str, library_name: str,
                       failed_checks: Dict, all_checks: Dict):
    """Fired when health checks failed — trash empty was skipped."""
    if not webhook_url:
        return
    failed_list = "\n".join(
        f"• **{n}**: {c['detail']}" for n, c in failed_checks.items()
    )
    _post(webhook_url, {"embeds": [{
        "title":       f"⚠️ {PRODUCT_NAME} — {instance_name} / {library_name}",
        "description": f"Health checks failed — trash empty skipped.\n\n**Failed:**\n{failed_list}",
        "color":       0xf06565,
        "fields":      _check_fields(all_checks),
    }]})


def notify_error(webhook_url: str, instance_name: str, library_name: str,
                 error: str, checks: Dict):
    """Fired when emptyTrash API call failed."""
    if not webhook_url:
        return
    _post(webhook_url, {"embeds": [{
        "title":       f"🔴 {PRODUCT_NAME} — {instance_name} / {library_name} error",
        "description": f"emptyTrash failed:\n```{error}```",
        "color":       0xe74c3c,
        "fields":      _check_fields(checks),
    }]})


def notify_skip(webhook_url: str, instance_name: str,
                library_name: str, reason: str):
    """Fired when run was skipped (scheduling paused, config error, etc)."""
    if not webhook_url:
        return
    _post(webhook_url, {"embeds": [{
        "title":       f"⏭️ {PRODUCT_NAME} — {instance_name} / {library_name} skipped",
        "description": f"**Reason:** {reason}",
        "color":       0xe8a045,
    }]})


def _checks_text(checks: Dict) -> str:
    if not checks:
        return ""
    return "\n".join(
        f"{'PASS' if result.get('pass') else 'FAIL'} — {name}: "
        f"{result.get('detail', '')}"
        for name, result in checks.items()
    )


def _apprise_delivery(destination, title: str, body: str,
                      notify_type: str = "info") -> bool:
    """Deliver to one Apprise URL without exposing its credentials in logs."""
    try:
        import apprise

        client = apprise.Apprise()
        if not client.add(destination.url):
            logger.warning(
                "Notification destination '%s' has an invalid Apprise URL",
                destination.name,
            )
            return False
        type_map = {
            "success": apprise.NotifyType.SUCCESS,
            "warning": apprise.NotifyType.WARNING,
            "failure": apprise.NotifyType.FAILURE,
            "info": apprise.NotifyType.INFO,
        }
        return bool(client.notify(
            title=_brand_text(title),
            body=_brand_text(body),
            notify_type=type_map.get(notify_type, apprise.NotifyType.INFO),
        ))
    except Exception as exc:
        logger.warning(
            "Notification destination '%s' failed: %s",
            destination.name,
            type(exc).__name__,
        )
        return False


def _apprise_fanout(config, event: str, title: str, body: str,
                    notify_type: str = "info") -> None:
    destinations = [
        destination
        for destination in config.notification_destinations
        if destination.enabled and event in destination.events and destination.url
    ]
    for destination in destinations:
        threading.Thread(
            target=_apprise_delivery,
            args=(destination, title, body, notify_type),
            name=f"notify-{destination.name or 'destination'}",
            daemon=True,
        ).start()


def _native_async(delivery, *args) -> None:
    threading.Thread(
        target=delivery,
        args=args,
        name="notify-discord",
        daemon=True,
    ).start()


def test_destination(destination) -> bool:
    """Synchronously send a harmless test message for Settings feedback."""
    return _apprise_delivery(
        destination,
        f"{PRODUCT_NAME} notification test",
        "This destination is configured correctly.",
        "info",
    )


def dispatch_emptied(config, instance_name: str, library_name: str,
                     removed_items: List[Dict], checks: Dict,
    breakdown: str = "") -> None:
    if config.discord_webhook:
        _native_async(
            notify_emptied,
            config.discord_webhook, instance_name, library_name,
            removed_items, checks, breakdown,
        )
    count = len(removed_items)
    details = breakdown or f"{count} item(s)"
    body = f"Emptied {details} from trash."
    item_lines = _removed_item_lines(removed_items)
    if item_lines:
        body += "\n\n" + "\n".join(item_lines)
    checks_text = _checks_text(checks)
    if checks_text:
        body += "\n\nChecks:\n" + checks_text
    _apprise_fanout(
        config, "emptied",
        f"{PRODUCT_NAME} — {instance_name} / {library_name}",
        body, "success",
    )


def dispatch_clean(config, instance_name: str, library_name: str,
                   checks: Dict) -> None:
    if config.discord_webhook:
        _native_async(
            notify_clean, config.discord_webhook, instance_name, library_name, checks,
        )
    body = "Trash was already empty — nothing to remove."
    checks_text = _checks_text(checks)
    if checks_text:
        body += "\n\nChecks:\n" + checks_text
    _apprise_fanout(
        config, "clean",
        f"{PRODUCT_NAME} — {instance_name} / {library_name}",
        body, "success",
    )


def dispatch_health_fail(config, instance_name: str, library_name: str,
                         failed_checks: Dict, all_checks: Dict) -> None:
    if config.discord_webhook:
        _native_async(
            notify_health_fail,
            config.discord_webhook, instance_name, library_name,
            failed_checks, all_checks,
        )
    failed = "\n".join(
        f"{name}: {result.get('detail', '')}"
        for name, result in failed_checks.items()
    )
    body = "Health checks failed — trash empty skipped."
    if failed:
        body += "\n\nFailed:\n" + failed
    checks_text = _checks_text(all_checks)
    if checks_text:
        body += "\n\nAll checks:\n" + checks_text
    _apprise_fanout(
        config, "health_fail",
        f"{PRODUCT_NAME} warning — {instance_name} / {library_name}",
        body, "failure",
    )


def dispatch_error(config, instance_name: str, library_name: str,
                   error: str, checks: Dict) -> None:
    if config.discord_webhook:
        _native_async(
            notify_error,
            config.discord_webhook, instance_name, library_name, error, checks,
        )
    body = f"emptyTrash failed:\n{error}"
    checks_text = _checks_text(checks)
    if checks_text:
        body += "\n\nChecks:\n" + checks_text
    _apprise_fanout(
        config, "error",
        f"{PRODUCT_NAME} error — {instance_name} / {library_name}",
        body, "failure",
    )


def dispatch_skip(config, instance_name: str, library_name: str,
                  reason: str) -> None:
    if config.discord_webhook:
        _native_async(
            notify_skip, config.discord_webhook, instance_name, library_name, reason,
        )
    _apprise_fanout(
        config, "skip",
        f"{PRODUCT_NAME} skipped — {instance_name} / {library_name}",
        f"Reason: {reason}", "warning",
    )
