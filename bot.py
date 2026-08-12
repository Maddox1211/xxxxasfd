from __future__ import annotations

import asyncio
import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

MODERATOR_FORUM_ID = 1530486834495356940
APPEAL_REVIEW_CHANNEL_ID = 1535766234535034941
PUBLIC_REVIEWED_BANS_CHANNEL_ID = 1534808151629758604

TEST_CHANNEL_ID = 1535753623060091020
TEST_FORUM_ID = 1535756698189561926

MODERATOR_ROLE_IDS = {
    1523420007415939132,
    1523420156913778929,
    1534130096183443577,
    1523402078201057531,
    1535753661412544642,
    1523419081079001329,
}

AUTHORIZED_ROLE_IDS = {
    *MODERATOR_ROLE_IDS,
}

DEMOTE_ALLOWED_ROLE_IDS = {
    1523420007415939132,
    1523420156913778929,
    1534130096183443577,
    1523402078201057531,
    1529936496608678072,
}

DEMOTABLE_ROLE_IDS = {
    1523419081079001329,
    1523419421450829996,
    1534194372449534164,
}

DEMOTED_ROLE_ID = 1537199121667072061

PUNISHMENTS_FILE = "punishments.json"

TIMEOUT_DURATIONS = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

SOFTBAN_UNBAN_DELTAS = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

DURATION_CHOICES = [
    app_commands.Choice(
        name="1 Hour",
        value="1h",
    ),
    app_commands.Choice(
        name="1 Day",
        value="1d",
    ),
    app_commands.Choice(
        name="7 Days",
        value="7d",
    ),
    app_commands.Choice(
        name="30 Days",
        value="30d",
    ),
    app_commands.Choice(
        name="Permanent",
        value="permanent",
    ),
]


class PunishmentStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.records: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self.records = {}
            return

        with open(self.path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()

        if not raw:
            self.records = {}
            return

        try:
            self.records = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(
                f"Warning: {self.path} contains invalid JSON "
                f"({exc}). Starting with an empty store."
            )
            self.records = {}

    def _save_sync(self) -> None:
        temp_path = f"{self.path}.tmp"

        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(
                self.records,
                handle,
                indent=2,
            )

        os.replace(temp_path, self.path)

    async def add(self, **fields) -> dict:
        record = {
            "punishment_id": uuid.uuid4().hex,
            "appeal_status": "not_eligible",
            "appeal_reason": None,
            "appeal_message_id": None,
            "appeal_channel_id": None,
            "decided_by": None,
            "decision_note": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        record.update(fields)

        async with self.lock:
            self.records[record["punishment_id"]] = record
            self._save_sync()

        return record

    async def update(
        self,
        punishment_id: str,
        **fields,
    ) -> None:
        async with self.lock:
            record = self.records.get(punishment_id)

            if record is None:
                return

            record.update(fields)
            self._save_sync()

    def get(self, punishment_id: str) -> Optional[dict]:
        return self.records.get(punishment_id)

    def for_user(self, user_id: int) -> list[dict]:
        records = [
            record
            for record in self.records.values()
            if record["user_id"] == user_id
        ]

        records.sort(
            key=lambda record: record["timestamp"],
            reverse=True,
        )

        return records


store = PunishmentStore(PUNISHMENTS_FILE)


class ModerationBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            description=(
                "A freshie you dont gank today is a freshie "
                "that ganks you tomorrow."
            ),
        )

        self.user_cache: dict[int, str] = {}

    async def setup_hook(self) -> None:
        for record in store.records.values():
            status = record.get("appeal_status")

            if status == "not_appealed":
                self.add_view(
                    AppealButtonView(
                        record["punishment_id"]
                    )
                )

            elif status == "pending":
                self.add_view(
                    AppealDecisionView(
                        record["punishment_id"]
                    )
                )

        test_guild = discord.Object(id=1523401616739537097)
        self.tree.copy_global_to(guild=test_guild)
        await self.tree.sync(guild=test_guild)

        refresh_user_cache.start()
        process_expired_softbans.start()


bot = ModerationBot()


def has_authorized_role(member: discord.Member) -> bool:
    return any(
        role.id in AUTHORIZED_ROLE_IDS
        for role in member.roles
    )


def has_admin_role(member: discord.Member) -> bool:
    return any(
        role.id in MODERATOR_ROLE_IDS
        for role in member.roles
    )

def has_demote_role(member: discord.Member) -> bool:
    return any(
        role.id in DEMOTE_ALLOWED_ROLE_IDS
        for role in member.roles
    )


def demote_only():
    async def predicate(
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.guild is None:
            return False

        member = interaction.guild.get_member(
            interaction.user.id
        )

        if member is None:
            return False

        return has_demote_role(member)

    return app_commands.check(predicate)

def moderator_only():
    async def predicate(
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.guild is None:
            return False

        member = interaction.guild.get_member(
            interaction.user.id
        )

        if member is None:
            return False

        return has_authorized_role(member)

    return app_commands.check(predicate)


def admin_only():
    async def predicate(
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.guild is None:
            return False

        member = interaction.guild.get_member(
            interaction.user.id
        )

        if member is None:
            return False

        return has_admin_role(member)

    return app_commands.check(predicate)


def is_appeal_eligible(
    action: str,
    duration: str,
) -> bool:
    if action in ("perm_ban", "softban"):
        return True

    if action == "timeout" and duration in {
        "1h",
        "1d",
        "7d",
        "30d",
        "permanent",
    }:
        return True

    return False


def get_forum() -> Optional[discord.ForumChannel]:
    forum = bot.get_channel(MODERATOR_FORUM_ID)

    if forum is None:
        forum = bot.get_channel(TEST_FORUM_ID)

    if isinstance(forum, discord.ForumChannel):
        return forum

    return None


async def cache_guild_members() -> None:
    for guild in bot.guilds:
        try:
            async for member in guild.fetch_members(limit=None):
                bot.user_cache[member.id] = str(member)

        except (
            discord.Forbidden,
            discord.HTTPException,
        ) as exc:
            print(
                f"Failed to cache members from "
                f"{guild.name}: {exc}"
            )

    print(
        f"Cached {len(bot.user_cache)} unique users."
    )


@tasks.loop(minutes=20)
async def refresh_user_cache() -> None:
    await cache_guild_members()


@refresh_user_cache.before_loop
async def before_cache_refresh() -> None:
    await bot.wait_until_ready()
    await cache_guild_members()


@tasks.loop(minutes=5)
async def process_expired_softbans() -> None:
    now = datetime.now(timezone.utc)

    for record in list(store.records.values()):
        if record.get("action") != "softban":
            continue

        if record.get("unbanned"):
            continue

        unban_at = record.get("unban_at")

        if not unban_at:
            continue

        try:
            unban_time = datetime.fromisoformat(
                unban_at
            )
        except ValueError:
            print(
                f"Invalid unban_at for "
                f"{record['punishment_id']}"
            )
            continue

        if unban_time > now:
            continue

        guild = bot.get_guild(
            record["guild_id"]
        )

        if guild is None:
            continue

        try:
            await guild.unban(
                discord.Object(
                    id=record["user_id"]
                ),
                reason="Softban duration expired",
            )

        except discord.NotFound:
            pass

        except discord.Forbidden as exc:
            print(
                f"Missing permission to unban "
                f"{record['user_id']}: {exc}"
            )
            continue

        except discord.HTTPException as exc:
            print(
                f"Failed to auto-unban "
                f"{record['user_id']}: {exc}"
            )
            continue

        await store.update(
            record["punishment_id"],
            unbanned=True,
            unbanned_at=now.isoformat(),
        )

        print(
            f"Automatically unbanned "
            f"{record['user_tag']} "
            f"({record['user_id']})"
        )


@process_expired_softbans.before_loop
async def before_process_expired_softbans() -> None:
    await bot.wait_until_ready()


async def resolve_user(
    guild: discord.Guild,
    target: str,
) -> Optional[discord.User]:
    target = target.strip()

    if target.isdigit():
        try:
            return await bot.fetch_user(
                int(target)
            )
        except discord.NotFound:
            return None

    if target.startswith("<@"):
        user_id = (
            target
            .replace("<@", "")
            .replace("!", "")
            .replace(">", "")
        )

        if user_id.isdigit():
            try:
                return await bot.fetch_user(
                    int(user_id)
                )
            except discord.NotFound:
                return None

    target_lower = target.lower()

    for member in guild.members:
        if (
            member.name.lower() == target_lower
            or member.display_name.lower() == target_lower
            or str(member).lower() == target_lower
        ):
            return member

    return None


async def resolve_target(
    guild: discord.Guild,
    user: str,
) -> Optional[discord.User]:
    return await resolve_user(
        guild,
        user,
    )


async def create_moderation_report(
    *,
    title: str,
    target: discord.User,
    moderation_type: str,
    duration: str,
    reason: str,
    moderator: discord.Member,
    proof_text: str,
    proof: Optional[discord.Attachment],
) -> discord.Thread:
    forum = get_forum()

    if forum is None:
        raise RuntimeError(
            "Configured moderator forum could not be found."
        )

    embed = discord.Embed(
        title="Moderation Report",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="User",
        value=(
            f"{target.mention}\n"
            f"`{target.id}`"
        ),
        inline=False,
    )

    embed.add_field(
        name="Moderation Type",
        value=moderation_type,
        inline=True,
    )

    embed.add_field(
        name="Duration",
        value=duration,
        inline=True,
    )

    embed.add_field(
        name="Moderator",
        value=(
            f"{moderator.mention}\n"
            f"`{moderator.id}`"
        ),
        inline=True,
    )

    embed.add_field(
        name="Reason",
        value=reason,
        inline=False,
    )

    embed.add_field(
        name="Proof",
        value=(
            proof_text
            or "No proof text provided."
        ),
        inline=False,
    )

    if proof is not None:
        embed.add_field(
            name="Proof Attachment",
            value=proof.url,
            inline=False,
        )

        if (
            proof.content_type
            and proof.content_type.startswith("image/")
        ):
            embed.set_image(
                url=proof.url
            )

    thread, _ = await forum.create_thread(
        name=title,
        embed=embed,
    )

    return thread


def build_appeal_embed(
    record: dict,
    appeal_reason: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="Punishment Appeal",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="User",
        value=(
            f"<@{record['user_id']}>\n"
            f"`{record['user_id']}`"
        ),
        inline=False,
    )

    embed.add_field(
        name="Punishment",
        value=record["action_label"],
        inline=True,
    )

    embed.add_field(
        name="Duration",
        value=record["duration"],
        inline=True,
    )

    embed.add_field(
        name="Original Reason",
        value=record["reason"],
        inline=False,
    )

    embed.add_field(
        name="Appeal Reason",
        value=appeal_reason,
        inline=False,
    )

    embed.add_field(
        name="Proof",
        value=(
            record.get("proof_text")
            or "No proof text provided."
        ),
        inline=False,
    )

    if record.get("proof_url"):
        embed.add_field(
            name="Proof Attachment",
            value=record["proof_url"],
            inline=False,
        )

    return embed


class AppealModal(
    discord.ui.Modal,
    title="Submit Appeal",
):
    reason = discord.ui.TextInput(
        label="Why should this punishment be reversed?",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )

    def __init__(
        self,
        punishment_id: str,
    ) -> None:
        super().__init__()
        self.punishment_id = punishment_id

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        record = store.get(
            self.punishment_id
        )

        if (
            record is None
            or record["appeal_status"]
            != "not_appealed"
        ):
            await interaction.response.send_message(
                "This punishment is no longer eligible for appeal.",
                ephemeral=True,
            )
            return

        channel = bot.get_channel(
            APPEAL_REVIEW_CHANNEL_ID
        )

        if (
            channel is None
            or not isinstance(
                channel,
                discord.TextChannel,
            )
        ):
            await interaction.response.send_message(
                "Your appeal could not be submitted. "
                "The review channel is unavailable.",
                ephemeral=True,
            )
            return

        embed = build_appeal_embed(
            record,
            str(self.reason),
        )

        view = AppealDecisionView(
            self.punishment_id
        )

        message = await channel.send(
            embed=embed,
            view=view,
        )

        await store.update(
            self.punishment_id,
            appeal_status="pending",
            appeal_reason=str(self.reason),
            appeal_message_id=message.id,
            appeal_channel_id=channel.id,
        )

        await interaction.response.send_message(
            "Your appeal has been submitted for review.",
            ephemeral=True,
        )


class AppealButtonView(discord.ui.View):
    def __init__(
        self,
        punishment_id: str,
    ) -> None:
        super().__init__(timeout=None)

        self.punishment_id = punishment_id

        button = discord.ui.Button(
            label="Appeal",
            style=discord.ButtonStyle.primary,
            custom_id=(
                f"appeal_button:{punishment_id}"
            ),
        )

        button.callback = self.on_click
        self.add_item(button)

    async def on_click(
        self,
        interaction: discord.Interaction,
    ) -> None:
        record = store.get(
            self.punishment_id
        )

        if record is None:
            await interaction.response.send_message(
                "This punishment record could not be found.",
                ephemeral=True,
            )
            return

        if record["user_id"] != interaction.user.id:
            await interaction.response.send_message(
                "This appeal does not belong to you.",
                ephemeral=True,
            )
            return

        if record["appeal_status"] != "not_appealed":
            await interaction.response.send_message(
                "You have already used your appeal for this punishment.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            AppealModal(
                self.punishment_id
            )
        )


class AppealDecisionNoteModal(
    discord.ui.Modal,
    title="Appeal Decision",
):
    note = discord.ui.TextInput(
        label="Moderator note",
        placeholder=(
            "Optional note explaining the decision..."
        ),
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
    )

    def __init__(
        self,
        punishment_id: str,
        decision: str,
    ) -> None:
        super().__init__()

        self.punishment_id = punishment_id
        self.decision = decision

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(
            interaction.user.id
        )

        if (
            member is None
            or not has_authorized_role(member)
        ):
            await interaction.response.send_message(
                "You don't have permission to decide appeals.",
                ephemeral=True,
            )
            return

        record = store.get(
            self.punishment_id
        )

        if record is None:
            await interaction.response.send_message(
                "This punishment record could not be found.",
                ephemeral=True,
            )
            return

        if record["appeal_status"] != "pending":
            await interaction.response.send_message(
                "This appeal has already been decided.",
                ephemeral=True,
            )
            return

        note = str(self.note).strip()

        await store.update(
            self.punishment_id,
            appeal_status=self.decision,
            decided_by=interaction.user.id,
            decision_note=note or None,
        )

        if self.decision == "accepted":
            await reverse_punishment(
                interaction.guild,
                record,
            )

        original_message = interaction.message

        if original_message is not None:
            embed = (
                original_message.embeds[0]
                if original_message.embeds
                else discord.Embed(
                    title="Punishment Appeal"
                )
            )

            embed.add_field(
                name="Decision",
                value=(
                    f"{self.decision.capitalize()} "
                    f"by {interaction.user.mention}"
                ),
                inline=False,
            )

            if note:
                embed.add_field(
                    name="Moderator Note",
                    value=note,
                    inline=False,
                )

            embed.color = (
                discord.Color.green()
                if self.decision == "accepted"
                else discord.Color.red()
            )

            disabled_view = AppealDecisionView(
                self.punishment_id
            )

            for item in disabled_view.children:
                item.disabled = True

            await interaction.response.edit_message(
                embed=embed,
                view=disabled_view,
            )

        else:
            await interaction.response.send_message(
                "Appeal decision recorded.",
                ephemeral=True,
            )

        await send_public_appeal_review(
            record=record,
            decision=self.decision,
            moderator=interaction.user,
            note=note,
        )

        target_user = bot.get_user(
            record["user_id"]
        )

        if target_user is None:
            try:
                target_user = await bot.fetch_user(
                    record["user_id"]
                )
            except discord.HTTPException:
                target_user = None

        if target_user is not None:
            try:
                message = (
                    f"Your appeal has been "
                    f"{self.decision}."
                )

                if (
                    self.decision == "accepted"
                    and record["action"]
                    in {
                        "perm_ban",
                        "softban",
                    }
                ):
                    message += (
                        "\nYour ban has been removed."
                    )

                if note:
                    message += (
                        f"\nModerator note: {note}"
                    )

                await target_user.send(message)

            except (
                discord.Forbidden,
                discord.HTTPException,
            ):
                pass


class AppealDecisionView(discord.ui.View):
    def __init__(
        self,
        punishment_id: str,
    ) -> None:
        super().__init__(timeout=None)

        self.punishment_id = punishment_id

        select = discord.ui.Select(
            placeholder="Select a decision",
            custom_id=(
                f"appeal_decision:{punishment_id}"
            ),
            options=[
                discord.SelectOption(
                    label="Accept",
                    value="accepted",
                ),
                discord.SelectOption(
                    label="Deny",
                    value="denied",
                ),
            ],
        )

        select.callback = self.on_select
        self.select = select
        self.add_item(select)

    async def on_select(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.guild is None:
            return

        member = interaction.guild.get_member(
            interaction.user.id
        )

        if (
            member is None
            or not has_authorized_role(member)
        ):
            await interaction.response.send_message(
                "You don't have permission to decide appeals.",
                ephemeral=True,
            )
            return

        record = store.get(
            self.punishment_id
        )

        if record is None:
            await interaction.response.send_message(
                "This punishment record could not be found.",
                ephemeral=True,
            )
            return

        if record["appeal_status"] != "pending":
            await interaction.response.send_message(
                "This appeal has already been decided.",
                ephemeral=True,
            )
            return

        decision = self.select.values[0]

        await interaction.response.send_modal(
            AppealDecisionNoteModal(
                punishment_id=self.punishment_id,
                decision=decision,
            )
        )


async def reverse_punishment(
    guild: discord.Guild,
    record: dict,
) -> None:
    action = record["action"]

    if action not in {
        "perm_ban",
        "softban",
    }:
        return

    try:
        await guild.unban(
            discord.Object(
                id=record["user_id"]
            ),
            reason=(
                "Punishment appeal accepted"
            ),
        )

        if action == "softban":
            await store.update(
                record["punishment_id"],
                unbanned=True,
                unbanned_at=(
                    datetime.now(timezone.utc)
                    .isoformat()
                ),
                unban_reason="Appeal accepted",
            )

    except discord.NotFound:
        if action == "softban":
            await store.update(
                record["punishment_id"],
                unbanned=True,
                unbanned_at=(
                    datetime.now(timezone.utc)
                    .isoformat()
                ),
            )

    except discord.HTTPException as exc:
        print(
            f"Failed to reverse punishment "
            f"{record['punishment_id']}: {exc}"
        )


async def send_public_appeal_review(
    *,
    record: dict,
    decision: str,
    moderator: discord.Member,
    note: str,
) -> None:
    channel = bot.get_channel(
        PUBLIC_REVIEWED_BANS_CHANNEL_ID
    )

    if (
        channel is None
        or not isinstance(
            channel,
            discord.TextChannel,
        )
    ):
        print(
            "Public reviewed bans channel "
            "could not be found."
        )
        return

    target_user = bot.get_user(
        record["user_id"]
    )

    if target_user is None:
        try:
            target_user = await bot.fetch_user(
                record["user_id"]
            )
        except discord.HTTPException:
            target_user = None

    if target_user is not None:
        user_value = (
            f"{target_user.mention}\n"
            f"`{target_user.id}`"
        )
    else:
        user_value = (
            f"<@{record['user_id']}>\n"
            f"`{record['user_id']}`"
        )

    embed = discord.Embed(
        title="Reviewed Punishment Appeal",
        color=(
            discord.Color.green()
            if decision == "accepted"
            else discord.Color.red()
        ),
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="User",
        value=user_value,
        inline=False,
    )

    embed.add_field(
        name="Punishment",
        value=record["action_label"],
        inline=True,
    )

    embed.add_field(
        name="Duration",
        value=record["duration"],
        inline=True,
    )

    embed.add_field(
        name="Original Reason",
        value=record["reason"],
        inline=False,
    )

    embed.add_field(
        name="Appeal Reason",
        value=(
            record.get("appeal_reason")
            or "No appeal reason provided."
        ),
        inline=False,
    )

    embed.add_field(
        name="Decision",
        value=decision.capitalize(),
        inline=True,
    )

    embed.add_field(
        name="Reviewed By",
        value=(
            f"{moderator.mention}\n"
            f"`{moderator.id}`"
        ),
        inline=True,
    )

    if note:
        embed.add_field(
            name="Moderator Note",
            value=note,
            inline=False,
        )

    await channel.send(
        embed=embed
    )


async def perform_moderation(
    guild: discord.Guild,
    target: discord.User,
    action: str,
    duration: str,
    reason: str,
) -> str:
    if action == "warn":
        return (
            f"Warning issued\n"
            f"User: {target} (`{target.id}`)\n"
            f"Reason: {reason}"
        )

    if action == "perm_ban":
        await guild.ban(
            target,
            reason=reason,
            delete_message_seconds=0,
        )

        return (
            f"Permanent ban applied\n"
            f"User: {target} (`{target.id}`)\n"
            f"Reason: {reason}"
        )

    if action == "softban":
        if duration not in SOFTBAN_UNBAN_DELTAS:
            raise ValueError(
                "Softban requires a valid temporary duration."
            )

        await guild.ban(
            target,
            reason=reason,
            delete_message_seconds=0,
        )

        unban_at = (
            datetime.now(timezone.utc)
            + SOFTBAN_UNBAN_DELTAS[duration]
        )

        return (
            f"Temporary ban applied\n"
            f"User: {target} (`{target.id}`)\n"
            f"Duration: {duration}\n"
            f"Unban at: "
            f"{discord.utils.format_dt(unban_at, 'F')}\n"
            f"Reason: {reason}"
        )

    if action == "kick":
        await guild.kick(
            target,
            reason=reason,
        )

        return (
            f"Kick applied\n"
            f"User: {target} (`{target.id}`)\n"
            f"Reason: {reason}"
        )

    if action == "timeout":
        if duration not in TIMEOUT_DURATIONS:
            raise ValueError(
                "Timeout requires a valid duration."
            )

        member = guild.get_member(
            target.id
        )

        if member is None:
            raise ValueError(
                "The user must still be in the server "
                "to receive a timeout."
            )

        until = (
            datetime.now(timezone.utc)
            + TIMEOUT_DURATIONS[duration]
        )

        await member.timeout(
            until,
            reason=reason,
        )

        return (
            f"Timeout applied\n"
            f"User: {target} (`{target.id}`)\n"
            f"Duration: {duration}\n"
            f"Reason: {reason}"
        )

    raise ValueError(
        f"Unknown moderation action: {action}"
    )


@bot.event
async def on_ready() -> None:
    print(
        f"Logged in as {bot.user} "
        f"({bot.user.id})"
    )

    print(
        f"Authorized roles: "
        f"{len(AUTHORIZED_ROLE_IDS)}"
    )

    print(
        f"Cached users: "
        f"{len(bot.user_cache)}"
    )


@bot.tree.command(
    name="help",
    description="Show the moderation bot commands.",
)
@admin_only()
async def help_command(
    interaction: discord.Interaction,
) -> None:
    embed = discord.Embed(
        title="Moderation Bot Help",
        description=(
            "Available moderation commands:"
        ),
        color=discord.Color.blue(),
    )

    embed.add_field(
            name="/moderate",
            value=(
                "Warn, ban, softban, kick, or "
                "timeout a user."
            ),
            inline=False,
        )

    embed.add_field(
        name="/demote",
        value=(
            "Remove a staff member's rank role(s) "
            "and assign the demoted role."
        ),
        inline=False,
    )

    embed.add_field(
        name="/cacheusers",
        value=(
            "Refresh the cached server users."
        ),
        inline=False,
    )

    embed.add_field(
        name="/userid",
        value=(
            "Look up a user's Discord ID "
            "from the cached usernames."
        ),
        inline=False,
    )

    embed.add_field(
        name="/lookupuser",
        value=(
            "View a user's punishment history."
        ),
        inline=False,
    )

    embed.add_field(
        name="/exportrecords",
        value=(
            "Export a user's punishment records "
            "as a JSON file."
        ),
        inline=False,
    )

    embed.add_field(
        name="/resetappeal",
        value=(
            "Reset appeal eligibility for one "
            "or all punishments belonging to a user."
        ),
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="cacheusers",
    description="Refresh the cached users.",
)
@moderator_only()
async def cacheusers(
    interaction: discord.Interaction,
) -> None:
    await interaction.response.defer(
        ephemeral=True
    )

    await cache_guild_members()

    await interaction.followup.send(
        f"Cached {len(bot.user_cache)} users.",
        ephemeral=True,
    )


@bot.tree.command(
    name="moderate",
    description="Moderate a user.",
)
@app_commands.describe(
    user="User mention, username, or Discord ID.",
    action="Moderation action.",
    duration=(
        "Duration. Not used for warnings or kicks."
    ),
    reason="Reason for the moderation.",
    proof_text="Optional text proof.",
    proof="Optional image or video proof.",
)
@app_commands.choices(
    action=[
        app_commands.Choice(
            name="Warn",
            value="warn",
        ),
        app_commands.Choice(
            name="Permanent Ban",
            value="perm_ban",
        ),
        app_commands.Choice(
            name="Softban",
            value="softban",
        ),
        app_commands.Choice(
            name="Kick",
            value="kick",
        ),
        app_commands.Choice(
            name="Timeout",
            value="timeout",
        ),
    ],
    duration=DURATION_CHOICES,
)
@moderator_only()
async def moderate(
    interaction: discord.Interaction,
    user: str,
    action: app_commands.Choice[str],
    duration: Optional[app_commands.Choice[str]] = None,
    reason: str = "No reason provided",
    proof_text: str = "",
    proof: Optional[discord.Attachment] = None,
) -> None:
    await interaction.response.defer(
        ephemeral=True
    )

    action_value = action.value

    if action_value in {
        "softban",
        "timeout",
    } and duration is None:
        await interaction.followup.send(
            "A duration is required for this action.",
            ephemeral=True,
        )
        return

    if (
        action_value == "softban"
        and duration is not None
        and duration.value == "permanent"
    ):
        await interaction.followup.send(
            "Softbans cannot be permanent. "
            "Use Permanent Ban instead.",
            ephemeral=True,
        )
        return

    if (
        action_value == "timeout"
        and duration is not None
        and duration.value == "permanent"
    ):
        await interaction.followup.send(
            "Discord does not support permanent timeouts. "
            "Use a temporary timeout or a ban.",
            ephemeral=True,
        )
        return

    target = await resolve_target(
        interaction.guild,
        user,
    )

    if target is None:
        await interaction.followup.send(
            "Could not find that user. "
            "Use their Discord ID if they left.",
            ephemeral=True,
        )
        return

    if target.id == bot.user.id:
        await interaction.followup.send(
            "I cannot moderate myself.",
            ephemeral=True,
        )
        return

    duration_value = (
        duration.value
        if duration is not None
        else "N/A"
    )

    duration_label = (
        duration.name
        if duration is not None
        else "N/A"
    )

    if action_value == "warn":
        duration_value = "N/A"
        duration_label = "N/A"

    if action_value == "kick":
        duration_value = "N/A"
        duration_label = "N/A"

    try:
        result = await perform_moderation(
            guild=interaction.guild,
            target=target,
            action=action_value,
            duration=duration_value,
            reason=reason,
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to perform "
            "that moderation action.",
            ephemeral=True,
        )
        return

    except discord.HTTPException as exc:
        await interaction.followup.send(
            f"Discord rejected the moderation action: "
            f"{exc}",
            ephemeral=True,
        )
        return

    except ValueError as exc:
        await interaction.followup.send(
            str(exc),
            ephemeral=True,
        )
        return

    title = f"{action.name}: {reason} | {target}"

    if len(title) > 100:
        title = title[:97] + "..."

    thread = None

    try:
        thread = await create_moderation_report(
            title=title,
            target=target,
            moderation_type=action.name,
            duration=duration_label,
            reason=reason,
            moderator=interaction.user,
            proof_text=proof_text,
            proof=proof,
        )

        report_note = (
            f"\nReport: {thread.mention}"
        )

    except (
        RuntimeError,
        discord.HTTPException,
    ) as exc:
        report_note = (
            f"\nReport failed: {exc}"
        )

    eligible = is_appeal_eligible(
        action_value,
        duration_value,
    )

    unban_at = None

    if (
        action_value == "softban"
        and duration_value
        in SOFTBAN_UNBAN_DELTAS
    ):
        unban_at = (
            datetime.now(timezone.utc)
            + SOFTBAN_UNBAN_DELTAS[
                duration_value
            ]
        ).isoformat()

    record = await store.add(
        user_id=target.id,
        user_tag=str(target),
        guild_id=interaction.guild.id,
        moderator_id=interaction.user.id,
        action=action_value,
        action_label=action.name,
        duration=duration_label,
        reason=reason,
        proof_text=proof_text,
        proof_url=(
            proof.url
            if proof is not None
            else None
        ),
        report_thread_id=(
            thread.id
            if thread is not None
            else None
        ),
        appeal_status=(
            "not_appealed"
            if eligible
            else "not_eligible"
        ),
        unban_at=unban_at,
        unbanned=(
            False
            if unban_at is not None
            else None
        ),
    )

    appeal_note = ""
    dm_note = ""

    if action_value == "warn":
        dm_embed = discord.Embed(
            title="You have received a warning",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        dm_embed.add_field(
            name="Server",
            value=interaction.guild.name,
            inline=False,
        )

        dm_embed.add_field(
            name="Reason",
            value=reason,
            inline=False,
        )

        try:
            await target.send(
                embed=dm_embed
            )

            dm_note = (
                "\nWarning DM sent to user."
            )

        except (
            discord.Forbidden,
            discord.HTTPException,
        ):
            dm_note = (
                "\nCould not DM the user."
            )

    elif eligible:
        dm_embed = discord.Embed(
            title="You have received a punishment",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )

        dm_embed.add_field(
            name="Server",
            value=interaction.guild.name,
            inline=False,
        )

        dm_embed.add_field(
            name="Punishment",
            value=action.name,
            inline=True,
        )

        dm_embed.add_field(
            name="Duration",
            value=duration_label,
            inline=True,
        )

        dm_embed.add_field(
            name="Reason",
            value=reason,
            inline=False,
        )

        dm_embed.add_field(
            name="Appeal",
            value=(
                "You may submit one appeal for "
                "this punishment using the button below."
            ),
            inline=False,
        )

        try:
            await target.send(
                embed=dm_embed,
                view=AppealButtonView(
                    record["punishment_id"]
                ),
            )

            appeal_note = (
                "\nAppeal notice sent to user."
            )

        except (
            discord.Forbidden,
            discord.HTTPException,
        ):
            appeal_note = (
                "\nCould not DM the user "
                "an appeal notice."
            )

    await interaction.followup.send(
        result
        + report_note
        + dm_note
        + appeal_note,
        ephemeral=True,
    )


@bot.tree.command(
    name="demote",
    description=(
        "Remove a staff member's rank role(s) and "
        "assign the demoted role."
    ),
)
@app_commands.describe(
    user="User mention, username, or Discord ID.",
)
@demote_only()
async def demote(
    interaction: discord.Interaction,
    user: str,
) -> None:
    await interaction.response.defer(
        ephemeral=True
    )

    target = await resolve_target(
        interaction.guild,
        user,
    )

    if target is None:
        await interaction.followup.send(
            "Could not find that user. "
            "Use their Discord ID if they left.",
            ephemeral=True,
        )
        return

    member = interaction.guild.get_member(
        target.id
    )

    if member is None:
        await interaction.followup.send(
            "That user must still be in the server "
            "to be demoted.",
            ephemeral=True,
        )
        return

    matched_roles = [
        role
        for role in member.roles
        if role.id in DEMOTABLE_ROLE_IDS
    ]

    if not matched_roles:
        await interaction.followup.send(
            f"{member} does not hold any of the "
            "demotable roles.",
            ephemeral=True,
        )
        return

    demoted_role = interaction.guild.get_role(
        DEMOTED_ROLE_ID
    )

    if demoted_role is None:
        await interaction.followup.send(
            "The demoted role could not be found "
            "on this server.",
            ephemeral=True,
        )
        return

    try:
        await member.remove_roles(
            *matched_roles,
            reason=(
                f"Demoted by {interaction.user}"
            ),
        )

        await member.add_roles(
            demoted_role,
            reason=(
                f"Demoted by {interaction.user}"
            ),
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to change "
            "that user's roles.",
            ephemeral=True,
        )
        return

    except discord.HTTPException as exc:
        await interaction.followup.send(
            f"Discord rejected the role change: {exc}",
            ephemeral=True,
        )
        return

    removed_names = ", ".join(
        role.name for role in matched_roles
    )

    await interaction.followup.send(
        f"Demoted {member}.\n"
        f"Removed: {removed_names}\n"
        f"Added: {demoted_role.name}",
        ephemeral=True,
    )

@bot.tree.command(
    name="userid",
    description=(
        "Look up a user's ID from a cached username."
    ),
)
@app_commands.describe(
    username="Username to search for.",
)
@moderator_only()
async def userid(
    interaction: discord.Interaction,
    username: str,
) -> None:
    await interaction.response.defer(
        ephemeral=True
    )

    query = username.strip().lower()

    if not query:
        await interaction.followup.send(
            "Provide a username to search for.",
            ephemeral=True,
        )
        return

    matches = sorted(
        (
            (user_id, name)
            for user_id, name
            in bot.user_cache.items()
            if query in name.lower()
        ),
        key=lambda item: item[1].lower(),
    )

    if not matches:
        await interaction.followup.send(
            "No cached user matched that username. "
            "Try /cacheusers to refresh the cache.",
            ephemeral=True,
        )
        return

    limit = 15

    lines = [
        f"{name} - `{user_id}`"
        for user_id, name in matches[:limit]
    ]

    result = "\n".join(lines)

    if len(matches) > limit:
        result += (
            f"\n...and {len(matches) - limit} "
            "more matches."
        )

    await interaction.followup.send(
        result,
        ephemeral=True,
    )


@bot.tree.command(
    name="lookupuser",
    description=(
        "Look up all punishment records for a user."
    ),
)
@app_commands.describe(
    user="User mention, username, or Discord ID.",
)
@moderator_only()
async def lookupuser(
    interaction: discord.Interaction,
    user: str,
) -> None:
    await interaction.response.defer(
        ephemeral=True
    )

    target = await resolve_target(
        interaction.guild,
        user,
    )

    if target is None:
        await interaction.followup.send(
            "Could not find that user. "
            "Use their Discord ID if they left.",
            ephemeral=True,
        )
        return

    records = store.for_user(
        target.id
    )

    if not records:
        await interaction.followup.send(
            f"No punishment records found for {target}.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=f"Punishment Record: {target}",
        description=(
            f"Total punishments: {len(records)}"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    for record in records[:10]:
        value = (
            f"ID: `{record['punishment_id']}`\n"
            f"Reason: {record['reason']}\n"
            f"Duration: {record['duration']}\n"
            f"Moderator: "
            f"<@{record['moderator_id']}>\n"
            f"Appeal Status: "
            f"{record['appeal_status']}\n"
            f"Date: "
            f"{record['timestamp'][:10]}"
        )

        if record.get("unban_at"):
            if record.get("unbanned"):
                unban_line = "Unbanned"
            else:
                unban_line = (
                    "Unban scheduled: "
                    f"{record['unban_at'][:10]}"
                )

            value += f"\n{unban_line}"

        if record.get("decision_note"):
            value += (
                "\nDecision Note: "
                f"{record['decision_note']}"
            )

        embed.add_field(
            name=record["action_label"],
            value=value,
            inline=False,
        )

    if len(records) > 10:
        embed.set_footer(
            text=(
                f"Showing 10 of "
                f"{len(records)} records."
            )
        )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="exportrecords",
    description=(
        "Export a user's punishment records "
        "as a JSON file."
    ),
)
@app_commands.describe(
    user="User mention, username, or Discord ID.",
)
@moderator_only()
async def exportrecords(
    interaction: discord.Interaction,
    user: str,
) -> None:
    await interaction.response.defer(
        ephemeral=True
    )

    target = await resolve_target(
        interaction.guild,
        user,
    )

    if target is None:
        await interaction.followup.send(
            "Could not find that user. "
            "Use their Discord ID if they left.",
            ephemeral=True,
        )
        return

    records = store.for_user(
        target.id
    )

    if not records:
        await interaction.followup.send(
            f"No punishment records found for {target}.",
            ephemeral=True,
        )
        return

    payload = json.dumps(
        records,
        indent=2,
    )

    buffer = io.BytesIO(
        payload.encode("utf-8")
    )

    filename = (
        f"punishments_{target.id}.json"
    )

    await interaction.followup.send(
        content=(
            f"Exported {len(records)} record(s) "
            f"for {target}."
        ),
        file=discord.File(
            fp=buffer,
            filename=filename,
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="resetappeal",
    description=(
        "Reset appeal eligibility for a user's punishment."
    ),
)
@app_commands.describe(
    user="User mention, username, or Discord ID.",
    punishment_id=(
        "Specific punishment ID to reset. "
        "Leave blank to reset all."
    ),
)
@admin_only()
async def resetappeal(
    interaction: discord.Interaction,
    user: str,
    punishment_id: Optional[str] = None,
) -> None:
    await interaction.response.defer(
        ephemeral=True
    )

    target = await resolve_target(
        interaction.guild,
        user,
    )

    if target is None:
        await interaction.followup.send(
            "Could not find that user. "
            "Use their Discord ID if they left.",
            ephemeral=True,
        )
        return

    records = [
        record
        for record in store.for_user(
            target.id
        )
        if record["appeal_status"]
        in (
            "pending",
            "accepted",
            "denied",
        )
    ]

    if punishment_id is not None:
        records = [
            record
            for record in records
            if record["punishment_id"]
            == punishment_id
        ]

    if not records:
        await interaction.followup.send(
            "No matching punishment records to reset.",
            ephemeral=True,
        )
        return

    for record in records:
        await store.update(
            record["punishment_id"],
            appeal_status="not_appealed",
            appeal_reason=None,
            appeal_message_id=None,
            appeal_channel_id=None,
            decided_by=None,
            decision_note=None,
        )

        bot.add_view(
            AppealButtonView(
                record["punishment_id"]
            )
        )

    await interaction.followup.send(
        f"Reset {len(records)} appeal record(s) "
        f"for {target}.",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(
        error,
        app_commands.CheckFailure,
    ):
        message = (
            "You don't have permission "
            "to use this command."
        )

        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
            )

        return

    print(
        f"Unhandled application command error: "
        f"{error}"
    )


if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set."
    )


bot.run(TOKEN)