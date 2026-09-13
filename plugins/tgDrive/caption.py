"""
Telegram caption composer with UTF-16 code unit budgeting.
Telegram measures caption limits in UTF-16 code units (max 1024 code units).
"""
import re
from typing import List, Optional

MAX_CAPTION_UTF16 = 1024


def utf16_len(s: str) -> int:
    """Return the length of string in UTF-16 code units (as counted by Telegram)."""
    return len(s.encode("utf-16-le")) // 2


def sanitize_hashtag(name: str) -> str:
    """
    Sanitize a name into a valid Telegram hashtag.
    Telegram hashtags allow letters, digits, and underscores.
    """
    if not name:
        return ""
    # Strip whitespace, replace hyphens and spaces with underscores
    cleaned = re.sub(r"[\s\-\.]+", "_", name.strip())
    # Remove any character that isn't alphanumeric or underscore
    cleaned = re.sub(r"[^\w]", "", cleaned)
    # Ensure it starts with '#' and is not just '#'
    if not cleaned:
        return ""
    return f"#{cleaned}"


def build_scene_caption(
    title: str,
    date: Optional[str] = None,
    studio: Optional[str] = None,
    performers: Optional[List[str]] = None,
    code: Optional[str] = None,
    tags: Optional[List[str]] = None,
    budget: int = MAX_CAPTION_UTF16,
) -> str:
    """
    Build human-readable caption for a scene video under the Telegram UTF-16 budget.
    Format:
    <Title> [(<Date>)]
    [Code: <Code>]
    [Studio: #<Studio>]
    [Performers: #<Performer1> #<Performer2>]
    [Tags: #<Tag1> #<Tag2>]
    """
    tags_list = []
    
    if code:
        ht = sanitize_hashtag(code)
        if ht:
            tags_list.append(ht)
    if studio:
        ht = sanitize_hashtag(studio)
        if ht:
            tags_list.append(ht)
    if performers:
        for p in performers:
            ht = sanitize_hashtag(p)
            if ht and ht not in tags_list:
                tags_list.append(ht)
    if tags:
        for t in tags:
            ht = sanitize_hashtag(t)
            if ht and ht not in tags_list:
                tags_list.append(ht)

    hashtags_str = " ".join(tags_list)
    date_str = f" ({date})" if date else ""
    
    # Bottom section: date + hashtags
    suffix_parts = []
    if date_str:
        suffix_parts.append(date_str.strip())
    if hashtags_str:
        suffix_parts.append(hashtags_str)
        
    suffix = "\n\n" + "\n".join(suffix_parts) if suffix_parts else ""
    suffix_len = utf16_len(suffix)

    available_for_title = budget - suffix_len
    if available_for_title < 10:
        # If hashtags/date take up almost all budget, prioritize title
        suffix = ""
        available_for_title = budget

    # Truncate title if needed
    curr_title = title or "Untitled Scene"
    if utf16_len(curr_title) > available_for_title:
        ellipsis = "..."
        target_len = available_for_title - utf16_len(ellipsis)
        # Character-by-character slicing until within budget
        truncated = ""
        for ch in curr_title:
            if utf16_len(truncated + ch) > target_len:
                break
            truncated += ch
        curr_title = truncated + ellipsis

    caption = curr_title + suffix
    # Final safeguard
    if utf16_len(caption) > budget:
        while utf16_len(caption) > budget and len(caption) > 0:
            caption = caption[:-1]
    return caption
