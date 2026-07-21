import re


def notification_label(item_id: int, definition: dict, item_name: str, stage: int = None) -> str:
    """
    Returns the string label for an item notification.
    E.g. "Heavy Cannon", "Progressive Ammo Upgrade (2/4)"
    """
    label = item_name
    
    # Remove AP color codes if any exist (though they shouldn't usually be here)
    label = re.sub(r'\{[^\}]+\}', '', label)
    
    if stage is not None and "max_count" in definition:
        max_count = definition["max_count"]
        if max_count > 1:
            label = f"{label} ({stage + 1}/{max_count})"
            
    return label.strip()

def notification_text(label: str) -> str:
    """
    Returns the formatted text for the notification.
    Rejects newlines and control characters.
    """
    if not label:
        raise ValueError("Notification label cannot be empty")
        
    text = f"AP: {label}"
    
    if '\n' in text or '\r' in text:
        raise ValueError("Newlines and control characters are not allowed in notifications")
        
    return text
