import contextlib
import logging

from efb_qq_slave import QQMessengerChannel
from ehforwarderbot import Chat
from ehforwarderbot.chat import GroupChat, PrivateChat, SystemChat
from ehforwarderbot.types import ChatID


class ChatManager:
    def __init__(self, channel: "QQMessengerChannel"):
        self.channel: "QQMessengerChannel" = channel
        self.logger: logging.Logger = logging.getLogger(__name__)

        self.MISSING_GROUP: GroupChat = GroupChat(
            channel=self.channel, uid=ChatID("__error_group__"), name="Group Missing"
        )

        self.MISSING_CHAT: PrivateChat = PrivateChat(
            channel=self.channel, uid=ChatID("__error_chat__"), name="Chat Missing"
        )

    async def build_efb_chat_as_private(self, context):
        """
        Build a EFB PrivateChat from a QQ context.

        + The uid of the chat is `private_<user_id>`.
        + The name of the chat is the nickname of the user.
        """

        uid = context["user_id"]
        if "sender" not in context or "nickname" not in context["sender"]:
            i: dict = await self.channel.QQClient.get_stranger_info(uid)
            chat_name = ""
            if i:
                chat_name = i["nickname"]
        else:
            chat_name = context["sender"]["nickname"]
        efb_chat = PrivateChat(
            channel=self.channel,
            uid="private" + "_" + str(uid),
            name=str(chat_name),
            alias=None if "alias" not in context else str(context["alias"]),
        )
        return efb_chat

    async def build_or_get_efb_member(self, chat: Chat, context):
        member_uid = context["user_id"]
        with contextlib.suppress(KeyError):
            return chat.get_member(str(member_uid))
        chat_name = ""
        if "nickname" not in context:
            i: dict = await self.channel.QQClient.get_stranger_info(member_uid)
            chat_name = ""
            if i:
                chat_name = i["nickname"]
        else:
            chat_name = context["nickname"]
        return chat.add_member(
            name=str(chat_name),
            alias=None if "alias" not in context else str(context["alias"]),
            uid=str(member_uid),
        )

    async def build_efb_chat_as_group(self, context, update_member=False):
        """
        Should be cached
        """

        is_discuss = False if context["message_type"] == "group" else True
        chat_uid = context["discuss_id"] if is_discuss else context["group_id"]
        efb_chat = GroupChat(channel=self.channel, uid=str(chat_uid))
        if not is_discuss:
            efb_chat.uid = "group" + "_" + str(chat_uid)
            i = await self.channel.QQClient.get_group_info(chat_uid)
            if i is not None:
                efb_chat.name = str(i["group_name"]) if "group_name" not in context else str(context["group_name"])
            else:
                efb_chat.name = str(chat_uid)
            efb_chat.vendor_specific = {"is_discuss": False}
            if update_member:
                members = await self.channel.QQClient.get_group_member_list(chat_uid, False)
                if members:
                    for member in members:
                        efb_chat.add_member(
                            name=str(member["card"]),
                            alias=str(member["nickname"]),
                            uid=str(member["user_id"]),
                        )
        else:
            efb_chat.uid = "discuss" + "_" + str(chat_uid)
            efb_chat.name = "Discuss Group" + "_" + str(chat_uid)
            # todo Find a way to distinguish from different discuss group
            efb_chat.vendor_specific = {"is_discuss": True}
        return efb_chat

    def build_efb_chat_as_anonymous_user(self, chat: Chat, context):
        """
        Build a EFB Member from a QQ group context if it is anonymous, the
        context["anonymous"] should be a dict with keys ["id", "name", "flag"].

        + The name of this member is "[Anonymous] {name}".
        + The uid of this chat is "anonymous_{flag}".
        + Use vendor_specific to store the anonymous_id.

        :param chat: The EFB Chat to add the member to
        :param context: The QQ context
        """

        anonymous_data = context["anonymous"]
        member_uid = "anonymous" + "_" + anonymous_data["flag"]
        with contextlib.suppress(KeyError):
            return chat.get_member(member_uid)
        chat_name = "[Anonymous] " + anonymous_data["name"]
        return chat.add_member(
            name=str(chat_name),
            alias=None if "alias" not in context else str(context["alias"]),
            uid=str(member_uid),
            vendor_specific={
                "is_anonymous": True,
                "anonymous_id": anonymous_data["id"],
            },
        )

    def build_efb_chat_as_system_user(self, context):
        """
        System user only!
        """

        return SystemChat(
            channel=self.channel,
            name=str(context["event_description"]),
            uid=ChatID("__{context[uid_prefix]}__".format(context=context)),
        )
