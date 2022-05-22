import asyncio
import copy
import functools
import logging
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union

import aiocqhttp
from aiocqhttp import CQHttp, Event
from efb_qq_slave import BaseClient, QQMessengerChannel
from ehforwarderbot import Chat, Message, MsgType, Status, coordinator
from ehforwarderbot.chat import (
    ChatMember,
    GroupChat,
    PrivateChat,
    SelfChatMember,
    SystemChatMember,
)
from ehforwarderbot.exceptions import (
    EFBChatNotFound,
    EFBMessageError,
    EFBOperationNotSupported,
)
from ehforwarderbot.message import MessageCommand, MessageCommands
from ehforwarderbot.status import MessageRemoval
from ehforwarderbot.types import ChatID, MessageID
from ehforwarderbot.utils import extra
from hypercorn.asyncio import serve
from hypercorn.config import Config as HyperConfig
from PIL import Image
from quart.logging import create_serving_logger
from requests import RequestException

from .ChatMgr import ChatManager
from .Exceptions import (
    CoolQAPIFailureException,
    CoolQDisconnectedException,
    CoolQOfflineException,
)
from .MsgDecorator import QQMsgProcessor
from .Utils import (
    async_send_messages_to_master,
    coolq_text_encode,
    download_file,
    download_group_avatar,
    download_user_avatar,
    process_quote_text,
    qq_emoji_list,
    strf_time,
)


class GoCQHttp(BaseClient):
    client_name: str = "GoCQHttp Client"
    client_id: str = "GoCQHttp"
    client_config: Dict[str, Any]

    coolq_bot: CQHttp = None
    logger: logging.Logger = logging.getLogger(__name__)
    channel: QQMessengerChannel

    friend_list: List[Dict] = []
    friend_dict: Dict[int, dict] = {}
    stranger_dict: Dict[int, dict] = {}
    group_list: List[Dict] = []
    group_dict: Dict[int, dict] = {}
    group_member_dict: Dict[int, Dict[str, Any]] = {}
    group_member_info_dict: Dict[Tuple[int, int], dict] = {}
    discuss_list: List[Dict] = []
    extra_group_list: List[Dict] = []
    repeat_counter = 0
    update_repeat_counter = 0

    def __init__(self, client_id: str, config: Dict[str, Any], channel):
        super().__init__(client_id, config)
        self.client_config = config[self.client_id]
        self.coolq_bot = CQHttp(
            api_root=self.client_config["api_root"],
            access_token=self.client_config["access_token"],
        )
        self.channel = channel
        self.chat_manager = ChatManager(channel)

        self.is_connected = False
        self.is_logged_in = False
        self.msg_decorator = QQMsgProcessor(instance=self)

        self.loop = asyncio.get_event_loop()
        self.shutdown_event = asyncio.Event()

        asyncio.set_event_loop(self.loop)

        async def forward_msgs_wrapper(msg_elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            fmt_msgs: List[Dict] = []
            for msg in msg_elements:
                from_user = await self.get_user_info(msg["sender"]["user_id"])
                header_text = {"data": {"text": f'{from_user["remark"]}（{from_user["nickname"]}）：\n'}, "type": "text"}
                footer_text = {"data": {"text": "\n- - - - - - - - - - - - - - -\n"}, "type": "text"}
                msg["content"].insert(0, header_text)
                msg["content"].append(footer_text)
                for i, inner_msg in enumerate(msg["content"]):
                    if "content" in inner_msg:
                        if i == 1:
                            fmt_msgs.pop()
                            msg["content"].pop()
                        fmt_msgs += await forward_msgs_wrapper([inner_msg])
                    else:
                        fmt_msgs.append(inner_msg)
            return fmt_msgs

        async def message_element_wrapper(
            context: Dict[str, Any], msg_element: Dict[str, Any], chat: Chat
        ) -> Tuple[str, List[Message], List[Tuple[Tuple[int, int], Union[Chat, ChatMember]]]]:
            msg_type = msg_element["type"]
            msg_data = msg_element["data"]
            main_text: str = ""
            messages: List[Message] = []
            at_list: List[Tuple[Tuple[int, int], Union[Chat, ChatMember]]] = []
            if msg_type == "text":
                main_text = msg_data["text"]
            elif msg_type == "face":
                qq_face = int(msg_data["id"])
                if qq_face in qq_emoji_list:
                    main_text = qq_emoji_list[qq_face]
                else:
                    main_text = "\u2753"  # ❓
            elif msg_type == "sface":
                main_text = "\u2753"  # ❓
            elif msg_type == "at":
                # todo Recheck if bug exists
                g_id = context["group_id"]
                my_uid = await self.get_qq_uid()
                self.logger.debug("My QQ uid: %s\n" "QQ mentioned: %s\n", my_uid, msg_data["qq"])
                if str(msg_data["qq"]) == "all":
                    group_card = "all"
                else:
                    member_info = (await self.get_user_info(msg_data["qq"], group_id=g_id))["in_group_info"]
                    group_card = member_info["card"] if member_info["card"] != "" else member_info["nickname"]
                self.logger.debug("Group card: {}".format(group_card))
                substitution_begin = len(main_text)
                substitution_end = len(main_text) + len(group_card) + 1
                main_text = "@{} ".format(group_card)
                if str(my_uid) == str(msg_data["qq"]) or str(msg_data["qq"]) == "all":
                    at_dict = ((substitution_begin, substitution_end), chat.self)
                    at_list.append(at_dict)
            elif msg_type == "reply":
                ref_user = await self.get_user_info(msg_data["qq"])
                main_text = (
                    f'「{ref_user["remark"]}（{ref_user["nickname"]}）：{msg_data["text"]}」\n'
                    "- - - - - - - - - - - - - - -\n"
                )
            elif msg_type == "forward":
                forward_msgs = (await self.coolq_api_query("get_forward_msg", message_id=msg_data["id"]))["messages"]
                logging.debug(f"Forwarded message: {forward_msgs}")
                fmt_forward_msgs = await forward_msgs_wrapper(forward_msgs)
                logging.debug(f"Formated forwarded message: {forward_msgs}")
                header_msg = {"data": {"text": "合并转发消息开始\n- - - - - - - - - - - - - - -\n"}, "type": "text"}
                footer_msg = {"data": {"text": "合并转发消息结束"}, "type": "text"}
                fmt_forward_msgs.insert(0, header_msg)
                fmt_forward_msgs.append(footer_msg)
                main_text, messages, _ = await message_elements_wrapper(context, fmt_forward_msgs, chat)
                return main_text, messages, []
            else:
                messages.extend(self.call_msg_decorator(msg_type, msg_data, chat))
            return main_text, messages, at_list

        async def message_elements_wrapper(
            context: Dict[str, Any], msg_elements: List[Dict[str, Any]], chat: Chat
        ) -> Tuple[str, List[Message], Dict[Tuple[int, int], Union[Chat, ChatMember]]]:
            messages: List[Message] = []
            main_text: str = ""
            at_dict: Dict[Tuple[int, int], Union[Chat, ChatMember]] = {}
            for msg_element in msg_elements:
                sub_main_text, sub_messages, sub_at_list = await message_element_wrapper(context, msg_element, chat)
                main_text_len = len(main_text)
                for at_tuple in sub_at_list:
                    pos = (
                        at_tuple[0][0] + main_text_len,
                        at_tuple[0][1] + main_text_len,
                    )
                    at_dict[pos] = at_tuple[1]
                main_text += sub_main_text
                messages.extend(sub_messages)
            return main_text, messages, at_dict

        @self.coolq_bot.on_message
        async def handle_msg(context: Event):
            self.logger.debug(repr(context))
            msg_elements = context["message"]
            qq_uid = context["user_id"]
            chat: Chat
            author: ChatMember

            user = await self.get_user_info(qq_uid)
            if context["message_type"] == "private":
                context["alias"] = user["remark"]
                chat: PrivateChat = await self.chat_manager.build_efb_chat_as_private(context)
            else:
                chat = await self.chat_manager.build_efb_chat_as_group(context)

            if "anonymous" not in context or context["anonymous"] is None:
                if context["message_type"] == "group":
                    if context["sub_type"] == "notice":
                        context["event_description"] = "System Notification"
                        context["uid_prefix"] = "group_notification"
                        author = chat.add_system_member(
                            name=context["event_description"],
                            uid=ChatID("__{context[uid_prefix]}__".format(context=context)),
                        )
                    else:
                        user = await self.get_user_info(qq_uid, group_id=context["group_id"])
                        context["nickname"] = user["remark"]
                        if user["is_in_group"]:
                            context["alias"] = user["in_group_info"]["card"]
                        else:
                            context["alias"] = user["remark"]
                        author = await self.chat_manager.build_or_get_efb_member(chat, context)
                elif context["message_type"] == "private":
                    author = chat.other
                else:
                    author = await self.chat_manager.build_or_get_efb_member(chat, context)
            else:  # anonymous user in group
                author = self.chat_manager.build_efb_chat_as_anonymous_user(chat, context)

            main_text, messages, at_dict = await message_elements_wrapper(context, msg_elements, chat)

            if main_text != "":
                messages.append(self.msg_decorator.qq_text_simple_wrapper(main_text, at_dict))
            coolq_msg_id = context["message_id"]
            for i in range(len(messages)):
                if not isinstance(messages[i], Message):
                    continue
                efb_msg: Message = messages[i]
                efb_msg.uid = (
                    f"{chat.uid.split('_')[-1]}_{coolq_msg_id}_{i}"
                    if i > 0
                    else f"{chat.uid.split('_')[-1]}_{coolq_msg_id}"
                )
                efb_msg.chat = chat
                efb_msg.author = author
                # if qq_uid != '80000000':

                # Append discuss group into group list
                if context["message_type"] == "discuss" and efb_msg.chat not in self.discuss_list:
                    self.discuss_list.append(efb_msg.chat)

                efb_msg.deliver_to = coordinator.master

                def send_message_wrapper(*args, **kwargs):
                    threading.Thread(target=async_send_messages_to_master, args=args, kwargs=kwargs).start()

                send_message_wrapper(efb_msg)

        @self.coolq_bot.on_notice("group_increase")
        async def handle_group_increase_msg(context: Event):
            context["event_description"] = "\u2139 Group Member Increase Event"
            if (context["sub_type"]) == "invite":
                text = "{nickname}({context[user_id]}) joined the group({group_name}) via invitation"
            else:
                text = "{nickname}({context[user_id]}) joined the group({group_name})"

            original_group = await self.get_group_info(context["group_id"], False)
            group_name = context["group_id"]
            if original_group is not None and "group_name" in original_group:
                group_name = original_group["group_name"]
            text = text.format(
                nickname=(await self.get_stranger_info(context["user_id"]))["nickname"],
                context=context,
                group_name=group_name,
            )

            context["message"] = text
            await self.send_efb_group_notice(context)

        @self.coolq_bot.on_notice("group_decrease")
        async def handle_group_decrease_msg(context: Event):
            context["event_description"] = "\u2139 Group Member Decrease Event"
            original_group = await self.get_group_info(context["group_id"], False)
            group_name = context["group_id"]
            if original_group is not None and "group_name" in original_group:
                group_name = original_group["group_name"]
            text = ""
            if context["sub_type"] == "kick_me":
                text = ("You've been kicked from the group({})").format(group_name)
            else:
                if context["sub_type"] == "leave":
                    text = "{nickname}({context[user_id]}) quited the group({group_name})"
                else:
                    text = "{nickname}({context[user_id]}) was kicked from the group({group_name})"
                text = text.format(
                    nickname=(await self.get_stranger_info(context["user_id"]))["nickname"],
                    context=context,
                    group_name=group_name,
                )
            context["message"] = text
            await self.send_efb_group_notice(context)

        @self.coolq_bot.on_notice("group_admin")
        async def handle_group_admin_msg(context: Event):
            context["event_description"] = "\u2139 Group Admin Change Event"
            if (context["sub_type"]) == "set":
                text = "{nickname}({context[user_id]}) has been appointed as the group({group_name}) administrator"
            else:
                text = "{nickname}({context[user_id]}) has been de-appointed as the group({group_name}) administrator"

            original_group = await self.get_group_info(context["group_id"], False)
            group_name = context["group_id"]
            if original_group is not None and "group_name" in original_group:
                group_name = original_group["group_name"]
            text = text.format(
                nickname=(await self.get_stranger_info(context["user_id"]))["nickname"],
                context=context,
                group_name=group_name,
            )

            context["message"] = text
            await self.send_efb_group_notice(context)

        @self.coolq_bot.on_notice("group_ban")
        async def handle_group_ban_msg(context: Event):
            context["event_description"] = "\u2139 Group Member Restrict Event"
            if (context["sub_type"]) == "ban":
                text = (
                    "{nickname}({context[user_id]}) "
                    "is restricted for speaking for {time} at the group({group_name}) by "
                    "{nickname_}({context[operator_id]})"
                )
                time_text = strf_time(context["duration"])
            else:
                text = (
                    "{nickname}({context[user_id]}) "
                    "is lifted from restrictions at the group({group_name}) by "
                    "{nickname_}({context[operator_id]}){time}"
                )
                time_text = ""

            original_group = await self.get_group_info(context["group_id"], False)
            group_name = context["group_id"]
            if original_group is not None and "group_name" in original_group:
                group_name = original_group["group_name"]
            text = text.format(
                nickname=(await self.get_stranger_info(context["user_id"]))["nickname"],
                context=context,
                time=time_text,
                group_name=group_name,
                nickname_=(await self.get_stranger_info(context["operator_id"]))["nickname"],
            )

            context["message"] = text
            await self.send_efb_group_notice(context)

        @self.coolq_bot.on_notice("offline_file")
        async def handle_offline_file_upload_msg(context: Event):
            context["event_description"] = "\u2139 Offline File Upload Event"
            context["uid_prefix"] = "offline_file"
            file_info_msg = ("Filename: {file[name]}\n" "File size: {file[size]}").format(file=context["file"])
            user = await self.get_user_info(context["user_id"])
            text = "{remark}({nickname}) uploaded a file to you\n"
            text = text.format(remark=user["remark"], nickname=user["nickname"]) + file_info_msg
            context["message"] = text
            self.send_msg_to_master(context)
            param_dict = {
                "context": context,
                "download_url": context["file"]["url"],
            }
            self.loop.run_in_executor(None, functools.partial(self.async_download_file, **param_dict))

        @self.coolq_bot.on_notice("group_upload")
        async def handle_group_file_upload_msg(context: Event):
            context["event_description"] = "\u2139 Group File Upload Event"
            context["uid_prefix"] = "group_upload"
            original_group = await self.get_group_info(context["group_id"], False)
            group_name = context["group_id"]
            if original_group is not None and "group_name" in original_group:
                group_name = original_group["group_name"]

            file_info_msg = ("File ID: {file[id]}\n" "Filename: {file[name]}\n" "File size: {file[size]}").format(
                file=context["file"]
            )
            member_info = (await self.get_user_info(context["user_id"], group_id=context["group_id"]))["in_group_info"]
            group_card = member_info["card"] if member_info["card"] != "" else member_info["nickname"]
            text = "{member_card}({context[user_id]}) uploaded a file to group({group_name})\n"
            text = text.format(member_card=group_card, context=context, group_name=group_name) + file_info_msg
            context["message"] = text
            await self.send_efb_group_notice(context)

            param_dict = {
                "context": context,
                "group_id": context["group_id"],
                "file_id": context["file"]["id"],
                "busid": context["file"]["busid"],
            }

            threading.Thread(target=self.async_download_group_file, args=[], kwargs=param_dict).start()

        @self.coolq_bot.on_notice("friend_add")
        async def handle_friend_add_msg(context: Event):
            context["event_description"] = "\u2139 New Friend Event"
            context["uid_prefix"] = "friend_add"
            text = "{nickname}({context[user_id]}) has become your friend!"
            text = text.format(
                nickname=(await self.get_stranger_info(context["user_id"]))["nickname"],
                context=context,
            )
            context["message"] = text
            self.send_msg_to_master(context)

        @self.coolq_bot.on_notice("group_recall")
        async def handle_group_recall_msg(context: Event):
            coolq_msg_id = context["message_id"]
            chat = GroupChat(channel=self.channel, uid=f"group_{context['group_id']}")

            efb_msg = Message(chat=chat, uid=MessageID(f"{chat.uid.split('_')[-1]}_{coolq_msg_id}"))
            coordinator.send_status(
                MessageRemoval(source_channel=self.channel, destination_channel=coordinator.master, message=efb_msg)
            )

        @self.coolq_bot.on_notice("friend_recall")
        async def handle_friend_recall_msg(context: Event):
            coolq_msg_id = context["message_id"]

            try:
                chat: PrivateChat = await self.chat_manager.build_efb_chat_as_private(context)
            except Exception:
                return
            efb_msg = Message(chat=chat, uid=MessageID(f"{chat.uid.split('_')[-1]}_{coolq_msg_id}"))
            coordinator.send_status(
                MessageRemoval(source_channel=self.channel, destination_channel=coordinator.master, message=efb_msg)
            )

        @self.coolq_bot.on_request("friend")  # Add friend request
        async def handle_add_friend_request(context: Event):
            self.logger.debug(repr(context))
            context["event_description"] = "\u2139 New Friend Request"
            context["uid_prefix"] = "friend_request"
            text = (
                "{nickname}({context[user_id]}) wants to be your friend!\n"
                "Here is the verification comment:\n"
                "{context[comment]}"
            )
            text = text.format(
                nickname=(await self.get_stranger_info(context["user_id"]))["nickname"],
                context=context,
            )
            context["message"] = text
            commands = [
                MessageCommand(
                    name=("Accept"),
                    callable_name="process_friend_request",
                    kwargs={"result": "accept", "flag": context["flag"]},
                ),
                MessageCommand(
                    name=("Decline"),
                    callable_name="process_friend_request",
                    kwargs={"result": "decline", "flag": context["flag"]},
                ),
            ]
            context["commands"] = commands
            self.send_msg_to_master(context)

        @self.coolq_bot.on_request("group")
        async def handle_group_request(context: Event):
            self.logger.debug(repr(context))
            context["uid_prefix"] = "group_request"
            context["group_name"] = ("[Request]") + (await self.get_group_info(context["group_id"]))["group_name"]
            context["group_id_orig"] = context["group_id"]
            context["group_id"] = str(context["group_id"]) + "_notification"
            context["message_type"] = "group"
            context["event_description"] = "\u2139 New Group Join Request"
            original_group = await self.get_group_info(context["group_id_orig"], False)
            group_name = context["group_id"]
            if original_group is not None and "group_name" in original_group:
                group_name = original_group["group_name"]
            msg = Message()
            msg.uid = "group" + "_" + str(context["group_id"])
            msg.author = (self.chat_manager.build_efb_chat_as_system_user(context)).other
            msg.chat = await self.chat_manager.build_efb_chat_as_group(context)
            msg.deliver_to = coordinator.master
            msg.type = MsgType.Text
            name = ""
            if not await self.get_friend_remark(context["user_id"]):
                name = "{}({})[{}] ".format(
                    (await self.get_stranger_info(context["user_id"]))["nickname"],
                    await self.get_friend_remark(context["user_id"]),
                    context["user_id"],
                )
            else:
                name = "{}[{}] ".format(
                    (await self.get_stranger_info(context["user_id"]))["nickname"],
                    context["user_id"],
                )
            msg.text = "{} wants to join the group {}({}). \nHere is the comment: {}".format(
                name, group_name, context["group_id_orig"], context["comment"]
            )
            msg.commands = MessageCommands(
                [
                    MessageCommand(
                        name=("Accept"),
                        callable_name="process_group_request",
                        kwargs={
                            "result": "accept",
                            "flag": context["flag"],
                            "sub_type": context["sub_type"],
                        },
                    ),
                    MessageCommand(
                        name=("Decline"),
                        callable_name="process_group_request",
                        kwargs={
                            "result": "decline",
                            "flag": context["flag"],
                            "sub_type": context["sub_type"],
                        },
                    ),
                ]
            )
            coordinator.send_message(msg)

        asyncio.run(self.check_status_periodically(run_once=True))

    def run_instance(self, host: str, port: int, debug: bool = False):
        def _run():
            config = HyperConfig()
            config.access_log_format = "%(h)s %(r)s %(s)s %(b)s %(D)s"
            config.accesslog = create_serving_logger()
            config.bind = [f"{host}:{port}"]
            if debug is not None:
                self.debug = debug
                config.use_reloader = debug
            config.errorlog = config.accesslog

            self.loop.create_task(serve(self.coolq_bot.server_app, config, shutdown_trigger=self.shutdown_event.wait))
            self.loop.create_task(self.check_status_periodically())
            self.loop.create_task(self.update_contacts_periodically())
            self.loop.run_forever()

        self.t = threading.Thread(target=_run)
        self.t.daemon = True
        self.t.start()

    def relogin(self):
        raise NotImplementedError

    def logout(self):
        raise NotImplementedError

    @extra(
        name=("Check CoolQ Status"),
        desc=("Force efb-qq-slave to refresh status from CoolQ Client.\n" "Usage: {function_name}"),
    )
    def login(self, param: str = ""):
        asyncio.run(self.check_status_periodically(run_once=True))
        return "Done"

    async def get_stranger_info(self, user_id: int, no_cache: bool = False) -> Dict[str, Any]:
        user_id = int(user_id)
        return await self.get_user_info(user_id, no_cache=no_cache)

    async def get_login_info(self) -> Dict[Any, Any]:
        res = await self.coolq_bot.get_status()
        if "good" in res or "online" in res:
            data = await self.coolq_bot.get_login_info()
            return {
                "status": 0,
                "data": {"uid": data["user_id"], "nickname": data["nickname"]},
            }
        else:
            return {"status": 1}

    async def get_groups(self) -> List:
        # todo Add support for discuss group iteration
        await self.update_group_list()  # Force update group list
        res = self.group_list
        # res = self.coolq_bot.get_group_list()
        groups = []
        for i in range(len(res)):
            context = {"message_type": "group", "group_id": res[i]["group_id"]}
            efb_chat = await self.chat_manager.build_efb_chat_as_group(context)
            groups.append(efb_chat)
        for i in range(len(self.extra_group_list)):
            does_exist = False
            for _j in range(len(res)):
                if str(self.extra_group_list[i]["group_id"]) == str(res[i]["group_id"]):
                    does_exist = True
                    break
            if does_exist:
                continue
            context = {
                "message_type": "group",
                "group_id": self.extra_group_list[i]["group_id"],
            }
            efb_chat = await self.chat_manager.build_efb_chat_as_group(context)
            groups.append(efb_chat)
        return groups + self.discuss_list

    async def get_friends(self) -> List:
        try:
            await self.update_friend_list()  # Force update friend list
        except CoolQAPIFailureException:
            self.deliver_alert_to_master(("Failed to retrieve the friend list.\n" "Only groups are shown."))
            return []
        users = []
        for current_user in self.friend_list:
            context = {
                "user_id": str(current_user["user_id"]),
                "nickname": current_user["nickname"],
                "alias": current_user["remark"],
            }
            efb_chat = await self.chat_manager.build_efb_chat_as_private(context)
            users.append(efb_chat)
        return users

    def receive_message(self):
        # Replaced by handle_msg()
        pass

    def send_message(self, msg: "Message") -> "Message":
        # todo Add support for edited message
        """
        self.logger.info("[%s] Sending message to WeChat:\n"
                         "uid: %s\n"
                         "Type: %s\n"
                         "Text: %s\n"
                         "Target Chat: %s\n"
                         "Target uid: %s\n",
                         msg.uid,
                         msg.chat.chat_uid, msg.type, msg.text, repr(msg.target.chat), msg.target.uid)
        """
        m = QQMsgProcessor(instance=self)
        chat_type = msg.chat.uid.split("_")

        self.logger.debug("[%s] Is edited: %s", msg.uid, msg.edit)
        if msg.edit:
            try:
                uid_type = msg.uid.split("_")
                asyncio.run(self.recall_message(uid_type[1]))
            except CoolQAPIFailureException:
                raise EFBOperationNotSupported(
                    ("Failed to recall the message!\n" "This message may have already expired.")
                )

        if msg.type in [MsgType.Text, MsgType.Link]:
            if msg.text == "kick`":
                group_id = chat_type[1]
                user_id = msg.target.author.uid
                asyncio.run(self.coolq_api_query("set_group_kick", group_id=group_id, user_id=user_id))
            else:
                if isinstance(msg.target, Message):
                    max_length = 50
                    tgt_text = coolq_text_encode(process_quote_text(msg.target.text, max_length))
                    tgt_alias = ""
                    if chat_type[0] != "private" and not isinstance(msg.target.author, SelfChatMember):
                        tgt_alias += m.coolq_code_at_wrapper(msg.target.author.uid)
                    else:
                        tgt_alias = ""
                    msg.text = "%s%s\n\n%s" % (
                        tgt_alias,
                        tgt_text,
                        coolq_text_encode(msg.text),
                    )
                msg.uid = asyncio.run(self.coolq_send_message(chat_type[0], chat_type[1], msg.text))
                self.logger.debug("[%s] Sent as a text message. %s", msg.uid, msg.text)
        elif msg.type in (MsgType.Image, MsgType.Sticker, MsgType.Animation):
            self.logger.info("[%s] Image/Sticker/Animation %s", msg.uid, msg.type)
            text = ""
            if msg.type != MsgType.Sticker:
                text += m.coolq_code_image_wrapper(msg.file, msg.path)
            else:
                with tempfile.NamedTemporaryFile(suffix=".gif") as f:
                    img = Image.open(msg.file)
                    try:
                        alpha = img.split()[3]
                        mask = Image.eval(alpha, lambda a: 255 if a <= 128 else 0)
                    except IndexError:
                        mask = Image.eval(img.split()[0], lambda a: 0)
                    img = img.convert("RGB").convert("P", palette=Image.ADAPTIVE, colors=255)
                    img.paste(255, mask)
                    img.save(f, transparency=255)
                    msg.file.close()
                    f.seek(0)
                    text += m.coolq_code_image_wrapper(f, f.name)
            if msg.text:
                msg.uid = asyncio.run(
                    self.coolq_send_message(chat_type[0], chat_type[1], text + coolq_text_encode(msg.text))
                )
            else:
                msg.uid = asyncio.run(self.coolq_send_message(chat_type[0], chat_type[1], text))
        # todo More MsgType Support
        elif msg.type is MsgType.Voice:
            text = m.coolq_voice_image_wrapper(msg.file, msg.path)
            msg.uid = asyncio.run(self.coolq_send_message(chat_type[0], chat_type[1], text))
            if msg.text:
                asyncio.run(self.coolq_send_message(chat_type[0], chat_type[1], msg.text))
        return msg

    def call_msg_decorator(self, msg_type: str, *args) -> List[Message]:
        try:
            func = getattr(self.msg_decorator, "qq_{}_wrapper".format(msg_type))
        except AttributeError:
            msg = f"Unsupported message type: {msg_type}"
            self.logger.error(msg)
            return self.msg_decorator.qq_unsupported_wrapper(msg)
        else:
            return func(*args)

    async def get_qq_uid(self):
        res = await self.get_login_info()
        if res["status"] == 0:
            return res["data"]["uid"]
        else:
            return None

    async def get_group_member_list(self, group_id, no_cache=False) -> List[Dict[str, Any]]:
        if (
            no_cache
            or (group_id not in self.group_member_dict)
            or (datetime.now() - self.group_member_dict[group_id]["time"] > timedelta(hours=1))
        ):  # Force Update
            try:
                member_list = await self.coolq_api_query("get_group_member_list", group_id=group_id, no_cache=no_cache)
            except CoolQAPIFailureException as e:
                self.deliver_alert_to_master(("Failed the get group member detail.") + "{}".format(e))
                return []
            self.group_member_dict[group_id] = {
                "members": member_list,
                "time": datetime.now(),
            }
        return self.group_member_dict[group_id]["members"]

    async def get_user_info(self, user_id: int, group_id: Optional[str] = None, no_cache=False):
        user_id = int(user_id)
        if no_cache or (not self.friend_list) or (user_id not in self.friend_dict):
            await self.update_friend_list()
        friend = self.friend_dict.get(user_id)
        if friend:
            user = copy.deepcopy(friend)
            user["is_friend"] = True
        else:
            if no_cache:
                stranger = await self.coolq_api_query("get_stranger_info", user_id=user_id)
                self.stranger_dict[user_id] = stranger
            stranger = self.stranger_dict.get(user_id)
            if stranger is None:
                stranger = await self.coolq_api_query("get_stranger_info", user_id=user_id)
                self.stranger_dict[user_id] = stranger
            user = copy.deepcopy(stranger)
            user["is_friend"] = False
        if group_id is not None:
            user["is_in_group"] = False
            member = next(
                filter(lambda member: member["user_id"] == user_id, await self.get_group_member_list(group_id)), None
            )
            if member is None:
                member = next(
                    filter(
                        lambda member: member["user_id"] == user_id,
                        await self.get_group_member_list(group_id, no_cache=True),
                    ),
                    None,
                )
            if member is not None:
                user["is_in_group"] = True
                user["in_group_info"] = member
        remark = user.get("remark")
        if not remark:
            user["remark"] = user["nickname"]
        return user

    async def get_group_info(self, group_id, no_cache=False):
        if no_cache or not self.group_list:
            await self.update_group_list()
        group = self.group_dict.get(group_id)
        if group:
            return group
        if no_cache:
            for extra_group in self.extra_group_list:
                if extra_group["group_id"] == group_id:
                    return extra_group
        try:
            external_group = await self.get_external_group_info(group_id)
        except CoolQAPIFailureException:
            self.logger.error(f"Get external group({group_id}) info failed.")
            return None
        else:
            self.extra_group_list.append(external_group)
            return external_group

    async def coolq_send_message(self, msg_type, uid, message):
        keyword = msg_type if msg_type != "private" else "user"
        res = await self.coolq_api_query("send_msg", message_type=msg_type, **{keyword + "_id": uid}, message=message)
        return str(uid) + "_" + str(res["message_id"])

    async def _coolq_api_wrapper(self, func_name, **kwargs):
        try:
            res = await self.coolq_bot.call_action(func_name, **kwargs)
        except RequestException as e:
            raise CoolQDisconnectedException(("Unable to connect to CoolQ Client!" "Error Message:\n{}").format(str(e)))
        except aiocqhttp.Error as ex:
            api_ex = CoolQAPIFailureException(
                ("CoolQ HTTP API encountered an error!\n" "Status Code:{} " "Return Code:{}").format(
                    ex.status_code, ex.retcode
                )
            )
            api_ex.status_code = ex.status_code
            api_ex.retcode = ex.retcode
            raise api_ex
        else:
            return res

    async def check_running_status(self):
        res = await self._coolq_api_wrapper("get_status")
        if res["good"] or res["online"]:
            return True
        else:
            raise CoolQOfflineException(("CoolQ Client isn't working correctly!"))

    async def coolq_api_query(self, func_name, **kwargs) -> Any:
        """# Do not call get_status too frequently
        if self.check_running_status():
            return self._coolq_api_wrapper(func_name, **kwargs)
        """
        if self.is_logged_in and self.is_connected:
            return await self._coolq_api_wrapper(func_name, **kwargs)
        elif self.repeat_counter < 3:
            self.deliver_alert_to_master(("Your status is offline.\n" "You may try login with /0_login"))
            self.repeat_counter += 1

    async def check_status_periodically(self, run_once: bool = False):
        interval = 300
        while True:
            self.logger.debug("Start checking status...")
            flag = True
            try:
                flag = await self.check_running_status()
            except CoolQDisconnectedException as e:
                if self.repeat_counter < 3:
                    self.deliver_alert_to_master(
                        (
                            "We're unable to communicate with CoolQ Client.\n"
                            "Please check the connection and credentials provided.\n"
                            "{}"
                        ).format(str(e))
                    )
                    self.repeat_counter += 1
                self.is_connected = False
                self.is_logged_in = False
                interval = 3600
            except (CoolQOfflineException, CoolQAPIFailureException):
                if self.repeat_counter < 3:
                    self.deliver_alert_to_master(
                        (
                            "CoolQ is running in abnormal status.\n"
                            "You may need to relogin your account "
                            "or have a check in CoolQ Client.\n"
                        )
                    )
                    self.repeat_counter += 1
                self.is_connected = True
                self.is_logged_in = False
                interval = 3600
            else:
                if not flag:
                    if self.repeat_counter < 3:
                        self.deliver_alert_to_master(
                            (
                                "We don't know why, but status check failed.\n"
                                "Please enable debug mode and consult the log "
                                "for more details."
                            )
                        )
                        self.repeat_counter += 1
                    self.is_connected = True
                    self.is_logged_in = False
                    interval = 3600
                else:
                    self.logger.debug("Status: OK")
                    self.is_connected = True
                    self.is_logged_in = True
                    self.repeat_counter = 0
            if run_once:
                return
            await asyncio.sleep(interval)

    def deliver_alert_to_master(self, message: str):
        context = {
            "message": message,
            "uid_prefix": "alert",
            "event_description": ("CoolQ Alert"),
        }
        self.send_msg_to_master(context)

    async def update_friend_list(self):
        self.friend_list = await self.coolq_api_query("get_friend_list")
        if self.friend_list:
            self.logger.debug("Update friend list completed. Entries: %s", len(self.friend_list))
            for friend in self.friend_list:
                if friend["remark"] == "":
                    friend["remark"] = friend["nickname"]
                self.friend_dict[friend["user_id"]] = friend
        else:
            self.logger.warning("Failed to update friend list")

    async def update_group_list(self):
        self.group_list = await self.coolq_api_query("get_group_list")
        if self.group_list:
            self.logger.debug("Update group list completed. Entries: %s", len(self.group_list))
            for group in self.group_list:
                self.group_dict[group["group_id"]] = group
        else:
            self.logger.warning("Failed to update group list")

    async def update_contacts_periodically(self, run_once: bool = False):
        interval = 1800
        while True:
            self.logger.debug("Start updating friend & group list")
            if self.is_connected and self.is_logged_in:
                try:
                    await self.update_friend_list()
                    await self.update_group_list()
                except CoolQAPIFailureException as ex:
                    if (ex.status_code) == 200 and (ex.retcode) == 104 and self.update_repeat_counter < 3:
                        self.send_cookie_expired_alarm()
                    if self.update_repeat_counter < 3:
                        self.deliver_alert_to_master(("Errors occurred when updating contacts: ") + (ex.message))
                        self.update_repeat_counter += 1
                else:
                    self.update_repeat_counter = 0
            self.logger.debug("Update completed")
            if run_once:
                return
            await asyncio.sleep(interval)

    async def get_friend_remark(self, uid):
        if (not self.friend_list) or (uid not in self.friend_dict):
            await self.update_friend_list()
        if uid not in self.friend_dict:
            return None  # I don't think you have such a friend
        return self.friend_dict[uid]["remark"]

    async def send_efb_group_notice(self, context):
        context["message_type"] = "group"
        self.logger.debug(repr(context))
        chat = await self.chat_manager.build_efb_chat_as_group(context)
        try:
            author = chat.get_member(SystemChatMember.SYSTEM_ID)
        except KeyError:
            author = chat.add_system_member()
        event_description = context.get("event_description", "")
        msg = Message(
            uid="__group_notice__.%s" % int(time.time()),
            type=MsgType.Text,
            chat=chat,
            author=author,
            text=(event_description + "\n\n" + context["message"]) if event_description else context["message"],
            deliver_to=coordinator.master,
        )
        coordinator.send_message(msg)

    def send_msg_to_master(self, context):
        self.logger.debug(repr(context))
        if not getattr(coordinator, "master", None):  # Master Channel not initialized
            raise Exception(context["message"])
        chat = self.chat_manager.build_efb_chat_as_system_user(context)
        try:
            author = chat.get_member(SystemChatMember.SYSTEM_ID)
        except KeyError:
            author = chat.add_system_member()
        msg = Message(
            uid="__{context[uid_prefix]}__.{uni_id}".format(context=context, uni_id=str(int(time.time()))),
            type=MsgType.Text,
            chat=chat,
            author=author,
            deliver_to=coordinator.master,
        )

        if "message" in context:
            msg.text = context["message"]
        if "commands" in context:
            msg.commands = MessageCommands(context["commands"])
        coordinator.send_message(msg)

    # As the old saying goes
    # A programmer spent 20% of time on coding
    # while the rest 80% on considering a variable/function/class name
    async def get_external_group_info(self, group_id):  # Special thanks to @lwl12 for thinking of this name
        res = await self.coolq_api_query("get_group_info", group_id=group_id)
        return res

    def send_status(self, status: "Status"):
        if isinstance(status, MessageRemoval):
            if not isinstance(status.message.author, SelfChatMember):
                raise EFBMessageError(("You can only recall your own messages."))
            try:
                uid_type = status.message.uid.split("_")
                asyncio.run(self.recall_message(uid_type[1]))
            except CoolQAPIFailureException:
                raise EFBMessageError(("Failed to recall the message. This message may have already expired."))
        else:
            raise EFBOperationNotSupported()
        # todo

    async def recall_message(self, message_id):
        await self.coolq_api_query("delete_msg", message_id=message_id)

    def send_cookie_expired_alarm(self):
        self.deliver_alert_to_master(
            (
                "Your cookie of CoolQ Client seems to be expired. "
                "Although it will not affect the normal functioning of sending/receiving "
                "messages, however, you may encounter issues like failing to retrieve "
                "friend list. Please consult "
                "https://github.com/milkice233/efb-qq-slave/wiki/Workaround-for-expired"
                "-cookies-of-CoolQ for solutions."
            )
        )

    async def process_friend_request(self, result, flag):
        res = "true" if result == "accept" else "false"
        try:
            await self.coolq_api_query("set_friend_add_request", approve=res, flag=flag)
        except CoolQAPIFailureException as e:
            return ("Failed to process request! Error Message:\n") + getattr(e, "message", repr(e))
        return "Done"

    async def process_group_request(self, result, flag, sub_type):
        res = "true" if result == "accept" else "false"
        try:
            await self.coolq_api_query("set_group_add_request", approve=res, flag=flag, sub_type=sub_type)
        except CoolQAPIFailureException as e:
            return ("Failed to process request! Error Message:\n") + getattr(e, "message", repr(e))
        return "Done"

    async def async_download_file(self, context, download_url):
        res = download_file(download_url)
        if isinstance(res, str):
            context["message"] = ("[Download] ") + res
            await self.send_efb_group_notice(context)
        elif res is None:
            pass
        else:
            data = {"file": res, "filename": context["file"]["name"]}
            context["message_type"] = "group"
            efb_msg = self.msg_decorator.qq_file_after_wrapper(data)
            efb_msg.uid = str(context["user_id"]) + "_" + str(uuid.uuid4()) + "_" + str(1)
            efb_msg.text = "Sent a file\n{}".format(context["file"]["name"])
            if context["uid_prefix"] == "offline_file":
                efb_msg.chat = asyncio.run(self.chat_manager.build_efb_chat_as_private(context))
            elif context["uid_prefix"] == "group_upload":
                efb_msg.chat = asyncio.run(self.chat_manager.build_efb_chat_as_group(context))
            efb_msg.author = asyncio.run(self.chat_manager.build_or_get_efb_member(efb_msg.chat, context))
            efb_msg.deliver_to = coordinator.master
            async_send_messages_to_master(efb_msg)

    async def async_download_group_file(self, context, group_id, file_id, busid):
        file = await self.coolq_api_query("get_group_file_url", group_id=group_id, file_id=file_id, busid=busid)
        download_url = file["url"]
        await self.async_download_file(context, download_url)

    def get_chat_picture(self, chat: "Chat") -> BinaryIO:
        chat_type = chat.uid.split("_")
        if chat_type[0] == "private":
            return download_user_avatar(chat_type[1])
        elif chat_type[0] == "group":
            return download_group_avatar(chat_type[1])
        else:
            return download_group_avatar("")

    def get_chats(self):
        async def _get_chats():
            return await asyncio.gather(self.get_friends(), self.get_groups())

        friend_chats, group_chats = asyncio.run(_get_chats())
        return friend_chats + group_chats

    def get_chat(self, chat_uid: ChatID) -> "Chat":
        # todo what is member_uid used for?
        chat_type = chat_uid.split("_")
        if chat_type[0] == "private":
            qq_uid = int(chat_type[1])
            remark = asyncio.run(self.get_friend_remark(qq_uid))
            context: Dict[str, Any] = {"user_id": qq_uid}
            if remark is not None:
                context["alias"] = remark
            return asyncio.run(self.chat_manager.build_efb_chat_as_private(context))
        elif chat_type[0] == "group":
            group_id = int(chat_type[1])
            context = {"message_type": "group", "group_id": group_id}
            return asyncio.run(self.chat_manager.build_efb_chat_as_group(context, update_member=True))
        elif chat_type[0] == "discuss":
            discuss_id = int(chat_type[1])
            context = {"message_type": "discuss", "discuss_id": discuss_id}
            return asyncio.run(self.chat_manager.build_efb_chat_as_group(context))
        raise EFBChatNotFound()

    async def check_self_update(self, run_once: bool = False):
        interval = 60 * 60 * 24
        while True:
            latest_version = self.channel.check_updates()
            if latest_version is not None:
                self.deliver_alert_to_master(
                    "New version({version}) of EFB-QQ-Slave has released! "
                    "Please manually update EQS by stopping ehForwarderbot first and then execute "
                    "<code>pip3 install --upgrade efb-qq-slave</code>".format(version=latest_version)
                )
            else:
                pass
            if run_once:
                return
            await asyncio.sleep(interval)

    def poll(self):
        asyncio.run(self.check_self_update(run_once=True))
        self.run_instance(
            host=self.client_config["host"],
            port=self.client_config["port"],
            debug=False,
        )

    def stop_polling(self):
        self.logger.debug("Gracefully stopping QQ Slave")
        self.shutdown_event.set()
        self.loop.stop()
        self.t.join()
