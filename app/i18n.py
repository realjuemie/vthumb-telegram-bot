"""Per-user copy for the Telegram bot and contact-sheet labels.

The phone's Telegram language only affects the slash menu (registered
separately). This catalog is the in-bot switch: replies, keyboards, and
the fixed labels drawn on the sheet. Default is Chinese so a missing
key does not change existing users.
"""

from __future__ import annotations

LANG_ZH = "zh"
LANG_EN = "en"
LANGS = (LANG_ZH, LANG_EN)
DEFAULT_LANG = LANG_ZH
LANG_LABELS = {
    LANG_ZH: "中文",
    LANG_EN: "English",
}

_STRINGS: dict[str, dict[str, str]] = {
    "cmd.start": {
        "zh": "开始使用并查看视频发送方式",
        "en": "Start and see how to send a video",
    },
    "cmd.help": {"zh": "查看使用说明", "en": "How to use this bot"},
    "cmd.status": {"zh": "查看缩略图设置和读取预算", "en": "Thumbnail settings and read budget"},
    "cmd.setting": {"zh": "选择每个用户独立的缩略图预设", "en": "Choose your own thumbnail preset"},
    "cmd.id": {"zh": "查看自己的 Telegram 用户 ID", "en": "Show your Telegram user ID"},
    "cmd.info": {"zh": "查看账号、授权状态和今日剩余次数", "en": "Account, access, and uses left today"},
    "cmd.checkin": {"zh": "每日签到，获得 1 到 3 次储存次数", "en": "Daily check-in for 1 to 3 stored credits"},
    "cmd.ad": {"zh": "看广告，获得 5 次储存次数", "en": "Watch an ad for stored credits"},
    "cmd.merge": {"zh": "合并接下来 N 条媒体 (默认 2)，图片在前视频在后", "en": "Merge the next N media (default 2)"},
    "cmd.thumb": {"zh": "回复一条视频，生成缩略图", "en": "Reply to a video to make a sheet"},
    "cmd.add": {"zh": "管理员：添加授权用户", "en": "Admin: allow a user"},
    "cmd.del": {"zh": "管理员：删除授权用户", "en": "Admin: remove a user"},
    "cmd.users": {"zh": "管理员：查看授权用户", "en": "Admin: list allowed users"},
    "cmd.guest": {"zh": "管理员：开关非授权用户使用", "en": "Admin: toggle guest access"},
    "cmd.bonus": {"zh": "管理员：给非授权用户增加储存次数", "en": "Admin: add stored credits for guests"},
    "cmd.announce": {"zh": "管理员：预览并发布 Markdown 公告", "en": "Admin: preview and publish an announcement"},
    "cmd.group": {"zh": "管理员：在群里授权该群使用缩略图", "en": "Admin: allow thumbnails in this group"},
    "menu.help": {"zh": "使用说明", "en": "Help"},
    "menu.setting": {"zh": "我的设置", "en": "Settings"},
    "menu.status": {"zh": "运行状态", "en": "Status"},
    "menu.info": {"zh": "我的信息", "en": "My info"},
    "menu.checkin": {"zh": "每日签到", "en": "Check in"},
    "menu.ad": {"zh": "看广告", "en": "Watch ad"},
    "menu.merge": {"zh": "合并消息", "en": "Merge"},
    "menu.merge_cancel": {"zh": "取消合并", "en": "Cancel merge"},
    "menu.users": {"zh": "授权名单", "en": "Allow list"},
    "menu.announce": {"zh": "发布公告", "en": "Announce"},
    "menu.guest": {"zh": "游客开关", "en": "Guest switch"},
    "menu.bonus": {"zh": "加次数", "en": "Add credits"},
    "pick.prompt": {
        "zh": "请选择语言。之后可以在设置里修改。\nChoose a language. You can change it later in Settings.",
        "en": "请选择语言。之后可以在设置里修改。\nChoose a language. You can change it later in Settings.",
    },
    "pick.saved": {"zh": "已设为中文。", "en": "Language set to English."},
    "lang.switched": {"zh": "语言已切换为中文。下方按钮已更新。", "en": "Language set to English. Bottom buttons updated."},
    "perm.denied": {
        "zh": "你还没有使用权限。\n你的 Telegram ID：{user_id}\n请将此 ID 提供给管理员添加。",
        "en": "You don't have access yet.\nYour Telegram ID: {user_id}\nSend this ID to an admin to be added.",
    },
    "perm.id_again": {"zh": "使用 /id 可再次查看 ID。", "en": "Use /id to see the ID again."},
    "perm.none": {"zh": "你没有使用权限。", "en": "You don't have access."},
    "unknown_id": {"zh": "未知", "en": "unknown"},
    "quota.exhausted": {
        "zh": "今天的 {limit} 次生成已经用完，储存次数也没有了。\n发送 /checkin 每天可再获得 1 到 3 次。\n",
        "en": "Today's {limit} new generations are used up, and you have no stored credits.\n/checkin adds 1 to 3 stored credits once a day.\n",
    },
    "quota.ad": {
        "zh": "看广告可获得 {reward} 次。点开广告后再点领取。\n",
        "en": "Watching an ad adds {reward} credits. Open the ad, then tap claim.\n",
    },
    "quota.contact": {
        "zh": "请通过社群群聊联系开发者升级权限。",
        "en": "Contact the developer in the community group to upgrade access.",
    },
    "guest.closed": {
        "zh": "非授权用户暂时不能使用。签到和生成都先停一下。",
        "en": "Guests are paused. Check-in and new sheets are off for now.",
    },
    "start.welcome": {
        "zh": "直接发送视频，或把视频作为文件发送，我会生成缩略图。\n不会预先完整下载大视频。\n\n下方按钮和 /help 里的命令是一样的。",
        "en": "Send a video, or send it as a file, and I'll make a thumbnail sheet.\nLarge videos are not downloaded in full first.\n\nThe buttons below do the same thing as the commands in /help.",
    },
    "help.lead": {"zh": "使用说明：\n", "en": "How to use:\n"},
    "help.1": {"zh": "1. 直接发送视频，或将视频作为文件发送。\n", "en": "1. Send a video, or send it as a file.\n"},
    "help.2": {"zh": "2. 使用 /setting 选择帧数与横竖布局。\n", "en": "2. Use /setting to choose frame count and grid.\n"},
    "help.3": {"zh": "3. 使用 /info 查看账号、授权状态和今日剩余次数。\n", "en": "3. Use /info for your account, access, and uses left today.\n"},
    "help.4": {
        "zh": "4. {small}MB 以内按需读取，必要时可读取完整范围。\n",
        "en": "4. Files up to {small}MB are read as needed, including the full range if required.\n",
    },
    "help.5": {
        "zh": "5. 大视频初始读取预算 {initial}，按需提升至 {hard}，且不超过 {cap}MB。\n",
        "en": "5. Large videos start at a {initial} read budget, rise to {hard} if needed, and never pass {cap}MB.\n",
    },
    "help.6": {"zh": "6. 已读取分块会临时缓存，任务结束后自动删除。\n", "en": "6. Fetched chunks are cached for the job, then deleted.\n"},
    "help.7": {"zh": "7. 多人同时发送会按到达顺序排队，轮到你时自动开始。\n", "en": "7. Simultaneous jobs wait in arrival order and start on their own.\n"},
    "help.8": {"zh": "8. 以文件发送的视频，缩略图完成后可合并成一条文件消息。\n", "en": "8. A file-sent video can be packed with its sheet into one file message.\n"},
    "help.9": {"zh": "9. 已授权的群里，回复视频并发送 /thumb。任务和私聊共用队列。", "en": "9. In an allowed group, reply to a video with /thumb. It shares the private queue."},
    "help.10": {
        "zh": "\n10. 点「看广告」后，先点广告，再点领取。到账后我会告诉你。",
        "en": "\n10. Tap Watch ad, open the ad, then tap claim. I'll tell you when the credits arrive.",
    },
    "help.buttons": {"zh": "\n\n也可以直接点下方按钮。", "en": "\n\nYou can also use the buttons below."},
    "help.admin": {
        "zh": "\n\n管理员命令：\n/add 用户ID\n/del 用户ID\n/users\n/guest on|off\n/bonus 次数\n/announce 公告内容\n/group 在群里授权该群\n/thumb 回复视频生成缩略图",
        "en": "\n\nAdmin commands:\n/add user-id\n/del user-id\n/users\n/guest on|off\n/bonus amount\n/announce text\n/group allows this group\n/thumb replies to a video",
    },
    "help.admin_buttons": {
        "zh": "\n授权名单、发布公告、游客开关、加次数也可以点下方按钮。/add、/del 需要带用户 ID。/bonus 后面跟次数，例如 /bonus 20。",
        "en": "\nAllow list, Announce, Guest switch, and Add credits are buttons too. /add and /del need a user ID. /bonus takes a number, for example /bonus 20.",
    },
    "help.guest": {
        "zh": "\n\n未授权每天可新生成 {limit} 次。使用以前的生成结果不计入次数。 /checkin 每天可额外获得 1 到 3 次，存在储存次数里。",
        "en": "\n\nGuests get {limit} new sheets a day. Reusing an old sheet is free. /checkin adds 1 to 3 stored credits once a day.",
    },
    "help.guest_ad": {
        "zh": " 看广告可获得 {reward} 次，也存在储存次数里。",
        "en": " An ad adds {reward} stored credits too.",
    },
    "id.line": {"zh": "你的 Telegram ID：{user_id}", "en": "Your Telegram ID: {user_id}"},
    "checkin.unavailable": {"zh": "签到暂时不可用。", "en": "Check-in is unavailable right now."},
    "checkin.already": {"zh": "今天已经签到过了。储存次数：{stored}", "en": "Already checked in today. Stored credits: {stored}"},
    "checkin.ok": {
        "zh": "签到成功，获得 {gained} 次。储存次数：{total}\n这些次数可以留着，不占每天的 {limit} 次。",
        "en": "Checked in, +{gained}. Stored credits: {total}\nThese don't count against the daily {limit}.",
    },
    "ad.disabled": {"zh": "广告功能未开启。", "en": "Ads are turned off."},
    "ad.not_live": {"zh": "看广告还未接入。", "en": "Ads are not connected yet."},
    "ad.not_connected": {"zh": "看广告还没接上，稍后再试。", "en": "Ads are not connected yet. Try again later."},
    "ad.none": {"zh": "暂时没有广告，请稍后再试。", "en": "No ad right now. Try again later."},
    "ad.watch": {
        "zh": "先点广告，再点领取。到账 {reward} 次后我会告诉你。",
        "en": "Open the ad, then tap claim. I'll tell you when {reward} credits arrive.",
    },
    "ad.credited": {"zh": "看广告到账，获得 {reward} 次。储存次数：{total}", "en": "Ad reward arrived, +{reward}. Stored credits: {total}"},
    "status.body": {
        "zh": "运行状态：正常\n缩略图：{preset} / {width}px\n发送方式：{delivery}\n分块缓存：{cache}MB 内存 + 临时磁盘去重\n小文件阈值：{small}MB\n大视频预算：初始 {initial}，按需至 {hard}，最高 {cap}MB\n任务队列：处理中 {running}，排队 {waiting}（并发 {concurrency}）",
        "en": "Status: ok\nSheet: {preset} / {width}px\nDelivery: {delivery}\nChunk cache: {cache}MB memory + temp disk dedupe\nSmall-file threshold: {small}MB\nLarge-video budget: {initial} at first, up to {hard}, cap {cap}MB\nQueue: running {running}, waiting {waiting} (concurrency {concurrency})",
    },
    "setting.title": {"zh": "缩略图设置（横x竖）", "en": "Thumbnail settings (columns x rows)"},
    "setting.layout": {"zh": "当前布局：{preset}", "en": "Layout: {preset}"},
    "setting.delivery": {"zh": "发送方式：{delivery}", "en": "Delivery: {delivery}"},
    "setting.theme": {"zh": "主题风格：{theme}", "en": "Theme: {theme}"},
    "setting.lang": {"zh": "语言：{language}", "en": "Language: {language}"},
    "setting.hint": {
        "zh": "提示：文件发送保留原图；媒体发送会被 Telegram 压缩。\n点击下方按钮切换，只影响你自己的任务。",
        "en": "File delivery keeps the original image. Media delivery is compressed by Telegram.\nButtons below change only your own jobs.",
    },
    "delivery.file": {"zh": "文件发送（原图）", "en": "File (original)"},
    "delivery.media": {"zh": "媒体发送（TG压缩）", "en": "Media (TG compress)"},
    "theme.potplayer": {"zh": "PotPlayer 风格", "en": "PotPlayer"},
    "theme.black_bg": {"zh": "黑底白字", "en": "Black background"},
    "theme.white_bg": {"zh": "白底黑字", "en": "White background"},
    "theme.pure_image": {"zh": "纯图无信息", "en": "Image only"},
    "theme.minimal_black": {"zh": "极简模式：黑", "en": "Minimal black"},
    "theme.minimal_white": {"zh": "极简模式：白", "en": "Minimal white"},
    "preset.frames": {"zh": "{count}帧 {cols}x{rows}", "en": "{count} frames {cols}x{rows}"},
    "fwdgate.on": {"zh": "任务转发管理员：开", "en": "Forward jobs to admin: on"},
    "fwdgate.off": {"zh": "任务转发管理员：关", "en": "Forward jobs to admin: off"},
    "fwdgate.opened": {"zh": "已打开", "en": "Turned on"},
    "fwdgate.closed": {"zh": "已关闭", "en": "Turned off"},
    "setting.saved": {"zh": "设置已保存。", "en": "Saved."},
    "setting.same": {"zh": "当前已是这个设置。", "en": "Already set."},
    "setting.bad": {"zh": "设置参数无效。", "en": "That setting is invalid."},
    "setting.bad_theme": {"zh": "主题无效。", "en": "That theme is invalid."},
    "setting.bad_delivery": {"zh": "发送方式无效。", "en": "That delivery mode is invalid."},
    "setting.no_right": {"zh": "没有权限。", "en": "Not allowed."},
    "admin.only": {"zh": "此命令仅限管理员使用。", "en": "Admins only."},
    "users.admins": {"zh": "管理员：{ids}", "en": "Admins: {ids}"},
    "users.allowed": {"zh": "授权用户：{ids}", "en": "Allowed users: {ids}"},
    "users.none": {"zh": "暂无", "en": "none"},
    "users.bad_format": {"zh": "格式错误，请使用 /{command} 用户ID", "en": "Bad format. Use /{command} user-id"},
    "users.added": {"zh": "已添加用户：{user_id}", "en": "Added user: {user_id}"},
    "users.already": {"zh": "用户已在授权名单中：{user_id}", "en": "Already allowed: {user_id}"},
    "users.admin_stuck": {"zh": "管理员由环境变量配置，不能通过 /del 删除。", "en": "Admins come from the environment and cannot be removed with /del."},
    "users.removed": {"zh": "已删除用户：{user_id}", "en": "Removed user: {user_id}"},
    "users.missing": {"zh": "用户不在授权名单中：{user_id}", "en": "Not on the allow list: {user_id}"},
    "guest.open": {"zh": "开放", "en": "open"},
    "guest.shut": {"zh": "已关闭", "en": "closed"},
    "guest.switch": {
        "zh": "非授权用户使用：{state}\n已记录的非授权用户：{known} 人\n授权用户和管理员不受影响。",
        "en": "Guest access: {state}\nKnown guests: {known}\nAllowed users and admins are unchanged.",
    },
    "bonus.ask": {
        "zh": "请发送要增加的次数，1 到 {max}。只加给已经出现过的非授权用户。",
        "en": "Send a number from 1 to {max}. It only goes to guests who have already appeared.",
    },
    "bonus.bad": {
        "zh": "格式错误，请使用 /bonus 次数，例如 /bonus 20。范围 1 到 {max}。",
        "en": "Bad format. Use /bonus amount, for example /bonus 20. Range 1 to {max}.",
    },
    "bonus.plain_bad": {
        "zh": "请发送 1 到 {max} 的整数，或点其他按钮取消。",
        "en": "Send an integer from 1 to {max}, or tap another button to cancel.",
    },
    "bonus.done": {
        "zh": "已给 {count} 个非授权用户各增加 {amount} 次储存次数。\n这些次数不占每天的 10 次，每天的额度用完后才会扣。\n只加给已经私聊过，或已经有次数、设置记录的人。之后新来的不会自动带上这次的次数。",
        "en": "Added {amount} stored credits to each of {count} guests.\nThese don't count against the daily 10, and are spent only after that daily allowance.\nOnly people who already chatted, have credits, or have settings. Later newcomers don't inherit this.",
    },
    "unknown_command": {"zh": "未知命令，请使用 /help 查看可用命令。", "en": "Unknown command. Use /help."},
    "group.admin_only": {"zh": "此命令仅限管理员使用。", "en": "Admins only."},
    "group.where": {"zh": "请在要授权的群里发送 /group。", "en": "Send /group inside the group you want to allow."},
    "group.not_ready": {"zh": "群授权还没准备好。", "en": "Group access isn't ready."},
    "group.revoked": {"zh": "已取消这个群的缩略图授权。", "en": "Thumbnail access removed for this group."},
    "group.was_off": {"zh": "这个群本来就没有授权。", "en": "This group was not allowed."},
    "group.allowed": {"zh": "已授权这个群。授权用户或管理员回复视频并发送 /thumb 即可生成。", "en": "This group is allowed. An allowed user or admin replies to a video with /thumb."},
    "group.already": {"zh": "这个群已经授权过了。", "en": "This group is already allowed."},
    "thumb.where": {"zh": "请在已授权的群里回复视频，再发送 /thumb。", "en": "Reply to a video in an allowed group, then send /thumb."},
    "thumb.not_allowed": {"zh": "这个群还没有授权。请管理员在群里发送 /group。", "en": "This group isn't allowed. An admin should send /group here."},
    "thumb.who": {"zh": "只有授权用户或管理员可以在群里生成缩略图。", "en": "Only an allowed user or an admin can make sheets in a group."},
    "thumb.reply": {"zh": "请回复一条视频，再发送 /thumb。", "en": "Reply to a video, then send /thumb."},
    "thumb.queue": {"zh": "任务队列还没准备好。", "en": "The job queue isn't ready."},
    "need.video": {"zh": "请发送视频文件，或把视频作为 document 发送给我。", "en": "Send a video file, or send the video as a document."},
    "merge.none": {"zh": "ℹ️ 没有等待中的合并任务。", "en": "ℹ️ No merge is waiting."},
    "merge.none_now": {"zh": "ℹ️ 当前没有等待中的合并任务。", "en": "ℹ️ No merge is waiting."},
    "merge.cancelled": {"zh": "✓ 已取消当前合并任务。", "en": "✓ Merge cancelled."},
    "merge.status": {
        "zh": "⏱ {got}/{expected} 已收, 还差 {remain} 条。\n5 秒静默后开始合并。\n取消: /merge cancel",
        "en": "⏱ {got}/{expected} received, {remain} to go.\nMerges after 5 seconds of quiet.\nCancel: /merge cancel",
    },
    "merge.busy": {
        "zh": "⚠️ 你已经有等待中的合并任务。\n先发完或发送 /merge cancel 取消。",
        "en": "⚠️ You already have a merge waiting.\nFinish it, or send /merge cancel.",
    },
    "merge.usage": {
        "zh": "✗ 用法: /merge [N | cancel | status]\n  N 范围: {lo}-{hi}。",
        "en": "✗ Usage: /merge [N | cancel | status]\n  N is {lo}-{hi}.",
    },
    "merge.bad_n": {"zh": "✗ N 必须是 {lo}-{hi} 之间的整数。", "en": "✗ N must be an integer from {lo} to {hi}."},
    "merge.armed": {
        "zh": "✓ 模式开启, 等待接下来 {n} 条媒体消息。\n  支持: 图片 / 视频 / 文件视频 / 动图 / 圆视频。\n  图片会排在前, 视频排在后, 自带的说明文字会被去除。\n  5 秒静默后开始合并。\n  取消: /merge cancel",
        "en": "✓ Waiting for the next {n} media messages.\n  Photos, videos, file-videos, GIFs, and video notes.\n  Photos first, videos last. Captions are dropped.\n  Merges after 5 seconds of quiet.\n  Cancel: /merge cancel",
    },
    "merge.wait_next": {"zh": "⏱ {got}/{expected}, 等待第 {next} 条...", "en": "⏱ {got}/{expected}, waiting for item {next}..."},
    "merge.soon": {"zh": "⏱ {got}/{expected}, 5 秒后开始合并...", "en": "⏱ {got}/{expected}, merging in 5 seconds..."},
    "merge.empty": {"zh": "✗ 没有可合并的媒体 (全部 file_id 缺失)。请重新 /merge。", "en": "✗ Nothing to merge (file ids missing). Send /merge again."},
    "merge.batch_fail": {"zh": "⚠️ 第 {start}-{end} 条合并失败: {error}", "en": "⚠️ Items {start}-{end} failed: {error}"},
    "merge.summary": {"zh": "✓ 已合并 {expected} 条 ({photos} 图 + {videos} 视频)", "en": "✓ Merged {expected} ({photos} photos + {videos} videos)"},
    "merge.sent": {"zh": "  成功发送 {sent}/{total} 条", "en": "  Sent {sent}/{total}"},
    "merge.stale": {
        "zh": "✗ 第 {indexes} 条 file_id 已失效 (Telegram 缓存已清理)。\n  请在 Telegram 长按原消息 → 引用回复 → 把那条转发给我。\n  然后重新发送 /merge 即可。",
        "en": "✗ Items {indexes} have expired file ids.\n  Long-press the original, reply to it, and forward it here.\n  Then send /merge again.",
    },
    "announce.hint": {
        "zh": "请发送公告内容。支持 Markdown：\n**粗体**、__斜体__、`代码`、[文字](链接)",
        "en": "Send the announcement. Markdown:\n**bold**, __italic__, `code`, [text](link)",
    },
    "announce.long": {"zh": "公告太长，请控制在 4000 字以内。", "en": "Too long. Keep it under 4000 characters."},
    "announce.preview": {"zh": "公告预览，确认后才会发给用户。\n\n", "en": "Preview. It is sent only after you confirm.\n\n"},
    "announce.confirm": {"zh": "确认发布", "en": "Publish"},
    "announce.edit": {"zh": "重新编辑", "en": "Edit again"},
    "announce.bad_md": {"zh": "Markdown 无法预览，请修改后再发。\n{error}", "en": "That Markdown can't be previewed. Edit it and send again.\n{error}"},
    "announce.not_yours": {"zh": "这不是你的公告。", "en": "That isn't your announcement."},
    "announce.send_new": {"zh": "请发送新内容", "en": "Send the new text"},
    "announce.publishing": {"zh": "开始发布", "en": "Publishing"},
    "announce.progress": {"zh": "正在发布公告… 共 {count} 人", "en": "Publishing… {count} people"},
    "announce.partial": {
        "zh": "公告只发出一部分。成功 {sent} 人，失败 {failed} 人，还有 {unsent} 人没发。\n账号还在被 Telegram 限制，预计还要等 {wait}，之后再发一次即可。",
        "en": "Only part of the announcement went out. Sent {sent}, failed {failed}, not attempted {unsent}.\nTelegram is still limiting this account for about {wait}. Send it again after that.",
    },
    "announce.blocked": {
        "zh": "公告没有发出。账号还在被 Telegram 限制，预计还要等 {wait}。",
        "en": "Nothing was sent. Telegram is still limiting this account for about {wait}.",
    },
    "announce.done": {"zh": "公告已发布。成功 {sent} 人，失败 {failed} 人。", "en": "Published. Sent {sent}, failed {failed}."},
    "info.title": {"zh": "账号信息", "en": "Account"},
    "info.name": {"zh": "名字：{name}", "en": "Name: {name}"},
    "info.unset": {"zh": "未设置", "en": "not set"},
    "info.username": {"zh": "用户名：{name}", "en": "Username: {name}"},
    "info.no_username": {"zh": "用户名：无", "en": "Username: none"},
    "info.id": {"zh": "Telegram ID：{user_id}", "en": "Telegram ID: {user_id}"},
    "info.grant_admin": {"zh": "授权用户：是（管理员）", "en": "Access: yes (admin)"},
    "info.grant_yes": {"zh": "授权用户：是", "en": "Access: yes"},
    "info.grant_no": {"zh": "授权用户：否", "en": "Access: no"},
    "info.unlimited": {"zh": "今日剩余次数：不限", "en": "New generations left today: unlimited"},
    "info.remaining": {"zh": "今日剩余次数：{remaining}/{limit}", "en": "New generations left today: {remaining}/{limit}"},
    "info.stored": {"zh": "储存次数：{stored}", "en": "Stored credits: {stored}"},
    "info.reuse_note": {
        "zh": "使用以前的生成记录不计入次数。每日签到获得的次数会存在这里。",
        "en": "Reusing an old sheet is free. Check-in credits are stored here.",
    },
    "info.closed": {"zh": "使用状态：已关闭", "en": "Access: paused"},
    "forward.done": {"zh": "用户 {label} (<a href=\"tg://user?id={user_id}\">{user_id}</a>) 完成 {count} 个任务", "en": "User {label} (<a href=\"tg://user?id={user_id}\">{user_id}</a>) finished {count} jobs"},
    "forward.done_plain": {"zh": "用户 {label} 完成 {count} 个任务", "en": "User {label} finished {count} jobs"},
    "pack.file": {
        "zh": "这个视频是按文件发送的，Telegram 不能直接内嵌播放。\n要不要把缩略图和原文件合并成一条？合并后仍是文件形式。",
        "en": "This video was sent as a file, so Telegram can't play it inline.\nPack the sheet and the original into one message? It stays a file.",
    },
    "pack.inline": {
        "zh": "要不要把缩略图和原视频合并成一条消息，方便转发？",
        "en": "Pack the sheet and the video into one message so it's easier to forward?",
    },
    "pack.yes": {"zh": "合并为一条", "en": "Pack into one"},
    "pack.no": {"zh": "不用了", "en": "No thanks"},
    "pack.expired_toast": {"zh": "按钮已失效。", "en": "That button expired."},
    "pack.offer_expired_toast": {"zh": "这条询问已过期。", "en": "That prompt expired."},
    "pack.offer_expired": {"zh": "这条合并询问已过期。", "en": "That pack prompt expired."},
    "pack.not_yours": {"zh": "这不是你的任务。", "en": "That isn't your job."},
    "pack.ok": {"zh": "好的", "en": "OK"},
    "pack.kept": {"zh": "好的，保持分开发送。", "en": "OK, leaving them separate."},
    "pack.as_file_toast": {"zh": "将以文件形式发送，请稍候", "en": "Sending as a file, please wait"},
    "pack.as_file": {
        "zh": "图片无法和文件视频合成相册，将以文件形式发送（仍是一条消息）。\n大文件可能较慢，请稍候…",
        "en": "A photo can't share an album with a file-video, so this goes out as files (still one message).\nLarge files can be slow…",
    },
    "pack.merging_toast": {"zh": "正在合并，请稍候", "en": "Packing, please wait"},
    "pack.merging": {"zh": "正在把缩略图和视频合成一条消息…", "en": "Packing the sheet and video into one message…"},
    "pack.fail": {"zh": "合并发送失败，请稍后重试。", "en": "Packing failed. Try again later."},
    "pack.done_file": {"zh": "已合并为一条文件消息，可长按转发。", "en": "Packed into one file message. Long-press to forward."},
    "pack.done_media": {"zh": "已合并为一条消息，可长按转发。", "en": "Packed into one message. Long-press to forward."},
    "reuse.prompt": {
        "zh": "这个视频已经有生成记录，别人生成过的也可以用。\n要直接使用之前的缩略图，还是重新生成？",
        "en": "This video already has a sheet, including one made by someone else.\nUse the previous sheet, or make a new one?",
    },
    "reuse.yes": {"zh": "使用之前的", "en": "Use previous"},
    "reuse.no": {"zh": "重新生成", "en": "Regenerate"},
    "reuse.sent_group": {"zh": "已把之前的缩略图发给你，并引用了原视频。这次不计入生成次数。", "en": "Sent the previous sheet, quoting the video. This doesn't use a credit."},
    "reuse.sent_private": {"zh": "已把之前的图片和视频合并成一条发给你。这次不计入生成次数。", "en": "Sent the previous sheet and video as one message. This doesn't use a credit."},
    "reuse.sent_toast": {"zh": "已发送", "en": "Sent"},
    "reuse.fail_toast": {"zh": "发送失败", "en": "Send failed"},
    "reuse.stale": {"zh": "之前的记录已失效，无法发送。可以再发一次并选择重新生成。", "en": "That saved sheet is gone. Send the video again and choose regenerate."},
    "reuse.quota": {"zh": "今天的次数已用完，可先签到", "en": "No generations left today. Check in first"},
    "reuse.quota_ad": {"zh": "或看广告", "en": " or watch an ad"},
    "reuse.go": {"zh": "开始重新生成", "en": "Regenerating"},
    "reuse.note_free": {"zh": "好的，重新生成。", "en": "OK, regenerating."},
    "reuse.note_paid": {"zh": "好的，重新生成。这次会计入次数。", "en": "OK, regenerating. This uses a credit."},
    "reuse.guest_closed": {"zh": "非授权用户暂时不能使用", "en": "Guests are paused"},
    "progress.metadata": {"zh": "正在读取视频信息", "en": "Reading video info"},
    "progress.frames": {"zh": "正在截取视频帧", "en": "Extracting frames"},
    "progress.compose": {"zh": "正在合成缩略图", "en": "Composing the sheet"},
    "progress.prepare_upload": {"zh": "正在准备上传图片", "en": "Preparing the image"},
    "progress.working": {"zh": "正在处理", "en": "Working"},
    "progress.upload": {"zh": "正在上传成品图片", "en": "Uploading the sheet"},
    "progress.pack": {"zh": "正在以文件形式发送（图片+原视频）", "en": "Sending as files (sheet + video)"},
    "progress.pack_media": {"zh": "正在合并为一条媒体消息", "en": "Packing into one media message"},
    "progress.remux_dl": {"zh": "正在下载原文件以便封装", "en": "Downloading the file to remux"},
    "progress.remux": {"zh": "正在无损封装为可播放视频", "en": "Remuxing to a playable video"},
    "queue.turn": {"zh": "轮到你了，开始处理。", "en": "Your turn. Starting."},
    "queue.joined": {
        "zh": "⏳ 已加入队列，按到达顺序处理。\n前面还有 {ahead} 个任务\n文件：{name}\n请稍候，轮到时会自动开始。",
        "en": "⏳ Queued in arrival order.\n{ahead} jobs ahead\nFile: {name}\nIt starts automatically when it's your turn.",
    },
    "queue.batch": {
        "zh": "已加入队列，按到达顺序处理。\n后面还有 {count} 个\n正在处理的会显示进度，轮到时自动开始。",
        "en": "Queued in arrival order.\n{count} more after this\nThe active job shows progress. The next one starts on its own.",
    },
    "flood.notice": {
        "zh": "Telegram 正在限制发送，预计 {when} 恢复。这段时间发视频不会开始生成，也不扣次数。到点后再发一次就行。",
        "en": "Telegram is limiting sends until about {when}. Videos sent before then won't start, and won't use a credit. Send again after that.",
    },
    "wait.soon": {"zh": "稍后", "en": "a moment"},
    "wait.minutes": {"zh": "{n} 分钟", "en": "{n} min"},
    "wait.hours": {"zh": "{n} 小时", "en": "{n} h"},
    "wait.hours_minutes": {"zh": "{hours} 小时 {minutes} 分钟", "en": "{hours} h {minutes} min"},
    "error.budget": {
        "zh": "这个视频的索引或关键帧不利于随机截取，已读取 {fetched}MB，达到自适应硬上限 {limit}MB。为避免接近全量下载，任务已主动停止。",
        "en": "This video's index or keyframes aren't suited to random seeks. Read {fetched}MB and hit the {limit}MB cap. Stopped to avoid downloading the whole file.",
    },
    "error.timeout": {"zh": "截帧超时，请稍后重试，或提高 FFMPEG_TIMEOUT。", "en": "Frame extract timed out. Try again later, or raise FFMPEG_TIMEOUT."},
    "error.unreadable": {"zh": "无法读取或截取这个视频。可能是格式、远程读取或代理连接问题。{suffix}", "en": "Couldn't read or extract this video. It may be the format, the remote read, or the proxy.{suffix}"},
    "error.ffmpeg": {"zh": "\n\nffmpeg 输出：\n{excerpt}", "en": "\n\nffmpeg output:\n{excerpt}"},
    "error.no_ffmpeg": {"zh": "\n\n（未捕获到 ffmpeg 错误输出，请查看容器日志）", "en": "\n\n(No ffmpeg error output. Check the service log.)"},
    "error.failed": {"zh": "生成失败，请查看容器日志中的错误类型。", "en": "Generation failed. Check the service log for the error type."},
    "error.prefix": {"zh": "处理失败：", "en": "Failed: "},
    "error.flood": {"zh": "处理失败：Telegram 限制发送，预计还要等 ", "en": "Failed: Telegram is limiting sends, about "},
    "done.sheet": {"zh": "处理完成，缩略图已发送。", "en": "Done. The sheet was sent."},
    "sheet.filename": {"zh": "文件名", "en": "Filename"},
    "sheet.size": {"zh": "大小", "en": "Size"},
    "sheet.resolution": {"zh": "分辨率", "en": "Resolution"},
    "sheet.video": {"zh": "视频解码器", "en": "Video codec"},
    "sheet.audio": {"zh": "音频解码器", "en": "Audio codec"},
    "sheet.duration": {"zh": "时长", "en": "Duration"},
}

USER_COMMAND_KEYS = (
    "start",
    "help",
    "status",
    "setting",
    "id",
    "info",
    "checkin",
    "ad",
    "merge",
    "thumb",
)
ADMIN_COMMAND_KEYS = ("add", "del", "users", "guest", "bonus", "announce", "group")
USER_MENU_KEYS = (
    ("help", "setting", "status"),
    ("info", "checkin", "ad"),
    ("merge", "merge_cancel"),
)
ADMIN_MENU_KEYS = (
    ("users", "announce"),
    ("guest", "bonus"),
)
MENU_COMMANDS = {
    "help": "/help",
    "setting": "/setting",
    "status": "/status",
    "info": "/info",
    "checkin": "/checkin",
    "ad": "/ad",
    "merge": "/merge",
    "merge_cancel": "/merge cancel",
    "users": "/users",
    "announce": "/announce",
    "guest": "/guest toggle",
    "bonus": "/bonus",
}
THEME_KEYS = {
    "potplayer": "theme.potplayer",
    "black_bg": "theme.black_bg",
    "white_bg": "theme.white_bg",
    "pure_image": "theme.pure_image",
    "minimal_black": "theme.minimal_black",
    "minimal_white": "theme.minimal_white",
}


def normalize_lang(lang: str | None) -> str:
    code = (lang or "").strip().lower().replace("_", "-")
    if code.startswith("en"):
        return LANG_EN
    return LANG_ZH


def t(lang: str | None, key: str, **kwargs: object) -> str:
    table = _STRINGS[key]
    template = table.get(normalize_lang(lang), table[DEFAULT_LANG])
    if not kwargs:
        return template
    return template.format(**kwargs)


def command_pairs(lang: str | None, *, include_admin: bool = False, ads_enabled: bool = True) -> tuple[tuple[str, str], ...]:
    keys = USER_COMMAND_KEYS + (ADMIN_COMMAND_KEYS if include_admin else ())
    pairs = []
    for name in keys:
        if name == "ad" and not ads_enabled:
            continue
        pairs.append((name, t(lang, f"cmd.{name}")))
    return tuple(pairs)


def menu_label(key: str, lang: str | None) -> str:
    return t(lang, f"menu.{key}")


def menu_actions() -> dict[str, str]:
    actions: dict[str, str] = {}
    for key, command in MENU_COMMANDS.items():
        for lang in LANGS:
            actions[menu_label(key, lang)] = command
    return actions


def menu_rows(is_admin: bool, lang: str | None = None, *, ads_enabled: bool = True) -> tuple[tuple[str, ...], ...]:
    rows: list[tuple[str, ...]] = []
    for key_row in USER_MENU_KEYS:
        labels = []
        for key in key_row:
            if key == "ad" and not ads_enabled:
                continue
            labels.append(menu_label(key, lang))
        if labels:
            rows.append(tuple(labels))
    if is_admin:
        for key_row in ADMIN_MENU_KEYS:
            rows.append(tuple(menu_label(key, lang) for key in key_row))
    return tuple(rows)


def theme_label(theme: str, lang: str | None) -> str:
    key = THEME_KEYS.get(theme)
    return t(lang, key) if key else theme


def delivery_label(delivery: str, lang: str | None) -> str:
    if delivery == "file":
        return t(lang, "delivery.file")
    return t(lang, "delivery.media")


def preset_label(count: int, cols: int, rows: int, lang: str | None) -> str:
    return t(lang, "preset.frames", count=count, cols=cols, rows=rows)


def format_wait(seconds: int, lang: str | None = None) -> str:
    if seconds <= 0:
        return t(lang, "wait.soon")
    minutes = (int(seconds) + 59) // 60
    if minutes < 60:
        return t(lang, "wait.minutes", n=minutes)
    hours, rest = divmod(minutes, 60)
    if rest:
        return t(lang, "wait.hours_minutes", hours=hours, minutes=rest)
    return t(lang, "wait.hours", n=hours)


def language_buttons():
    """Inline pickers. Imported lazily so tests can use i18n without Telethon buttons."""
    from telethon import Button

    return [[
        Button.inline(LANG_LABELS[LANG_ZH], b"pick:zh"),
        Button.inline(LANG_LABELS[LANG_EN], b"pick:en"),
    ]]
