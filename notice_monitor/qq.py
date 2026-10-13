"""Receipt-aware OneBot private messages and merged-forward delivery."""

import asyncio

from .sources import MonitorError


class DeliveryRejected(MonitorError):
    """The adapter explicitly rejected the operation; fallback is safe."""


class DeliveryUnknown(MonitorError):
    """The operation may have succeeded; do not immediately send it again."""


def plain(text):
    return {"type": "text", "data": {"text": text}}


class QQTransport:
    def __init__(self, call, user_id):
        self.call = call
        self.user_id = int(user_id)

    async def _call(self, action, **params):
        try:
            result = await asyncio.wait_for(self.call(action, **params), timeout=30)
        except DeliveryRejected:
            raise
        except Exception:
            raise DeliveryUnknown("QQ 请求结果未知，请稍后检查聊天记录。") from None
        if not isinstance(result, dict):
            raise DeliveryUnknown("QQ 未返回有效回执，请稍后检查聊天记录。")
        if result.get("status") == "async":
            raise DeliveryUnknown("QQ 请求仍在处理中，请稍后检查聊天记录。")
        if result.get("status") == "failed" or result.get("retcode", 0) not in (
            0,
            "0",
            None,
        ):
            raise DeliveryRejected("QQ 平台拒绝了请求。")
        return result.get("data", result)

    async def _send(self, action, **params):
        result = await self._call(action, user_id=self.user_id, **params)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id in (None, "", 0, "0"):
            raise DeliveryUnknown("QQ 未返回消息 ID，请稍后检查聊天记录。")
        return str(message_id)

    async def send_text(self, text):
        return await self._send("send_private_msg", message=[plain(text)])

    async def send_bundle(self, contents, self_id):
        """Fallback only for an explicit rejection, and only for the current batch."""
        for start in range(0, len(contents), 20):
            batch = contents[start : start + 20]
            nodes = [
                {
                    "type": "node",
                    "data": {
                        "uin": str(self_id),
                        "name": "哈工深通知",
                        "content": content,
                    },
                }
                for content in batch
            ]
            try:
                await self._send("send_private_forward_msg", messages=nodes)
            except DeliveryRejected:
                for content in batch:
                    await self._send("send_private_msg", message=content)

    async def previous_message_id(self, message_id, self_id):
        """Find a bot message immediately before this user's request.

        Args:
            message_id: ID of the incoming detail request.
            self_id: ID of the bot receiving that request.

        Returns:
            The preceding bot message ID, or None if adjacency is unconfirmed.
        """
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(0.3)
            try:
                result = await asyncio.wait_for(
                    self._call(
                        "get_friend_msg_history",
                        user_id=self.user_id,
                        count=20,
                        parse_mult_msg=False,
                        disable_get_url=True,
                    ),
                    timeout=3,
                )
            except DeliveryRejected:
                return None
            except (MonitorError, asyncio.TimeoutError):
                continue
            messages = result.get("messages") if isinstance(result, dict) else None
            if not isinstance(messages, list):
                return None
            try:
                # NapCat's message_seq is an opaque message ID. Only real_seq
                # represents the conversation order, including both senders.
                ordered = sorted(messages, key=lambda m: int(m["real_seq"]))
                ids = [str(m["message_id"]) for m in ordered]
                seqs = [int(m["real_seq"]) for m in ordered]
                senders = [str(m["sender"]["user_id"]) for m in ordered]
                if (
                    len(set(ids)) != len(ids)
                    or len(set(seqs)) != len(seqs)
                    or any(seq <= 0 for seq in seqs)
                    or any(m["message_type"] != "private" for m in ordered)
                    or any(
                        sender not in {str(self_id), str(self.user_id)}
                        for sender in senders
                    )
                    or any(
                        str(m.get("self_id", self_id)) != str(self_id) for m in ordered
                    )
                ):
                    return None
            except (KeyError, TypeError, ValueError):
                return None
            if str(message_id) not in ids:
                continue  # The incoming message may not be in QQ history yet.
            index = ids.index(str(message_id))
            if index == 0:
                continue
            if (
                senders[index] != str(self.user_id)
                or senders[index - 1] != str(self_id)
                or seqs[index] - seqs[index - 1] != 1
            ):
                return None  # Missing/recalled messages must not look adjacent.
            return ids[index - 1]
        return None

    async def read_own_text(self, message_id, self_id):
        result = await self._call("get_msg", message_id=int(message_id))
        if (
            not isinstance(result, dict)
            or str(result.get("sender", {}).get("user_id")) != str(self_id)
            or result.get("message_type") != "private"
        ):
            return ""
        if result.get("target_id") and str(result["target_id"]) != str(self.user_id):
            return ""
        message = result.get("message", [])
        if not isinstance(message, list):
            return ""
        return "".join(
            segment.get("data", {}).get("text", "")
            for segment in message
            if segment.get("type") == "text"
        )
