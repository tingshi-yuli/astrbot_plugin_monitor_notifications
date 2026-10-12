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
