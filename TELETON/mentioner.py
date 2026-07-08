from typing import List, Tuple
from telethon.tl.types import InputMessageEntityMentionName, InputUser

from models import ParsedUser
from spintax import spin_text


class Mentioner:
    def __init__(self, mentions_per_message: int = 2):
        self.mentions_per_message = mentions_per_message

    def build_mention_message(
        self,
        message_template: str,
        users: List[ParsedUser],
        base_entities: list | None = None,
        spin: bool = True,
    ) -> Tuple[str, list]:
        """
        Формирование сообщения с inline-упоминаниями.
        Возвращает (text, entities) для send_message().
        UTF-16 offsets для корректной работы с кириллицей и эмодзи.

        Стратегия (для снижения spam-pattern):
        - Если у пользователя есть username → пишем '@username' как обычный текст,
          Telegram сам сделает clickable mention. Не требует access_hash, выглядит
          естественно, не триггерит push-уведомление как агрессивный pseudo-link.
        - Если username нет → используем InputMessageEntityMentionName по user_id
          (требует access_hash, который должен быть в session-кэше).
        """
        text = spin_text(message_template) if spin else (message_template or "")
        text += "\n"
        entities = list(base_entities or [])

        for user in users:
            if user.username:
                # Текстовый @mention — Telegram резолвит сам, без entity
                text += f"@{user.username} "
            else:
                # Pseudo-link mention по user_id (требует access_hash в кэше)
                display_name = user.first_name or str(user.user_id)
                if getattr(user, "access_hash", 0):
                    offset = len(text.encode("utf-16-le")) // 2
                    length = len(display_name.encode("utf-16-le")) // 2
                    target = InputUser(user.user_id, int(getattr(user, "access_hash", 0) or 0))
                    entities.append(InputMessageEntityMentionName(
                        offset=offset,
                        length=length,
                        user_id=target,
                    ))
                text += display_name + " "

        return text.rstrip(), entities
