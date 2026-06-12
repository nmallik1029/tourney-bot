import uuid

import discord

from pug.config import VALID_REGIONS, BRAND, member_is_pug_staff
from pug.storage import (
    pug_data,
    save_pug_data,
    set_username,
    set_region,
    username_to_discord,
)

CASE_WARNING = "Your username is **CASE-SENSITIVE** - type it EXACTLY as it appears in Krunker (e.g. `xWater`, not `xwater`)."


# ── #account-link entry point ──────────────────────────────────────────────────
def build_account_link_embed() -> discord.Embed:
    embed = discord.Embed(
        title=f"Link Your {BRAND} Account",
        description=(
            "To play pugs you must link your Krunker account. You can link **one** account.\n\n"
            "Click **Link Your Account** below and provide:\n"
            "**1.** Your Krunker **username**\n"
            "**2.** Your **region** (NA / EU / ASIA / OCE / ME)\n\n"
            f"{CASE_WARNING}\n\n"
            "An admin will review and approve your request."
        ),
        color=0x5865F2,
    )
    return embed


class AccountLinkView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Link Your Account", style=discord.ButtonStyle.success, custom_id="pug_link_start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not pug_data["config"].get("review_channel_id"):
            await interaction.response.send_message(
                "Account linking isn't fully set up yet (no review channel). Ask an admin.", ephemeral=True
            )
            return
        await interaction.response.send_modal(LinkRequestModal())


class LinkRequestModal(discord.ui.Modal, title="Link Your Krunker Account"):
    username = discord.ui.TextInput(
        label="Krunker Username - CASE-SENSITIVE!!",
        placeholder="EXACT case, e.g. xWater not xwater",
        required=True, max_length=60,
    )
    region = discord.ui.TextInput(
        label="Region (NA / EU / ASIA / OCE / ME)", placeholder="NA", required=True, max_length=6
    )

    async def on_submit(self, interaction: discord.Interaction):
        region = self.region.value.strip().upper()
        if region not in VALID_REGIONS:
            await interaction.response.send_message(
                f"**{region}** isn't a valid region. Choose one of: {', '.join(VALID_REGIONS)}.", ephemeral=True
            )
            return

        username = self.username.value.strip()
        if not username:
            await interaction.response.send_message("Enter your username.", ephemeral=True)
            return

        rid = await post_link_request(interaction.client, interaction.user.id, username, region)
        if not rid:
            await interaction.response.send_message(
                "Couldn't submit - the review channel isn't set. Ask an admin.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Submitted for review! You'll be notified once an admin reviews it.", ephemeral=True
        )


# ── Admin review ────────────────────────────────────────────────────────────────
async def post_link_request(bot, user_id: int, username: str, region: str) -> str | None:
    """Post a review embed to the review channel with Accept/Deny buttons."""
    review_ch = bot.get_channel(pug_data["config"].get("review_channel_id"))
    if not review_ch:
        return None

    rid = uuid.uuid4().hex[:8]
    embed = discord.Embed(title="Account Link Request", color=0xFFA500)
    embed.add_field(name="User", value=f"<@{user_id}>", inline=False)
    embed.add_field(name="Username", value=f"`{username}`", inline=True)
    embed.add_field(name="Region", value=region, inline=True)
    embed.set_footer(text="Username is CASE-SENSITIVE - verify it matches Krunker exactly.")

    msg = await review_ch.send(embed=embed, view=LinkReviewView(rid))
    pug_data["link_requests"][rid] = {
        "user_id": user_id,
        "username": username,
        "region": region,
        "review_message_id": msg.id,
        "review_channel_id": review_ch.id,
    }
    save_pug_data()
    return rid


class LinkReviewView(discord.ui.View):
    def __init__(self, rid: str):
        super().__init__(timeout=None)
        self.add_item(LinkAcceptButton(rid))
        self.add_item(LinkDenyButton(rid))


class LinkAcceptButton(discord.ui.Button):
    def __init__(self, rid: str):
        super().__init__(label="Accept", style=discord.ButtonStyle.success, custom_id=f"linkok_{rid}")
        self.rid = rid

    async def callback(self, interaction: discord.Interaction):
        if not member_is_pug_staff(interaction.user):
            await interaction.response.send_message("Only staff can review link requests.", ephemeral=True)
            return
        req = pug_data["link_requests"].get(self.rid)
        if not req:
            await interaction.response.send_message("This request was already handled.", ephemeral=True)
            return

        # Ack immediately - the work below (Gist save + role changes) can exceed Discord's
        # 3-second interaction window, which is what caused "This interaction failed".
        await interaction.response.defer()

        uid = int(req["user_id"])
        username = req["username"]

        # One account per player; refuse if this username belongs to someone else.
        owner = username_to_discord(username)
        if owner is not None and owner != uid:
            await interaction.followup.send(
                f"`{username}` is already linked to <@{owner}>. Reset their profile "
                f"(`/pug-reset-profile`) first, then Accept again.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        set_username(uid, username)   # replaces any existing account
        set_region(uid, req["region"])
        pug_data["link_requests"].pop(self.rid, None)
        save_pug_data()

        member = interaction.guild.get_member(uid) if interaction.guild else None
        if member:
            from pug.roles import apply_region_role
            await apply_region_role(interaction.guild, member, req["region"])

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = 0x3FB950
        embed.add_field(name="Status", value=f"Accepted by {interaction.user.mention}", inline=False)
        for item in self.view.children:
            item.disabled = True
        try:
            await interaction.message.edit(embed=embed, view=self.view)
        except discord.HTTPException:
            pass

        try:
            user = await interaction.client.fetch_user(uid)
            await user.send(
                f"Your **{BRAND}** account link was **approved**! "
                f"Linked: `{username}` ({req['region']}). You can now queue."
            )
        except discord.HTTPException:
            pass


class LinkDenyButton(discord.ui.Button):
    def __init__(self, rid: str):
        super().__init__(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"linkno_{rid}")
        self.rid = rid

    async def callback(self, interaction: discord.Interaction):
        if not member_is_pug_staff(interaction.user):
            await interaction.response.send_message("Only staff can review link requests.", ephemeral=True)
            return
        if self.rid not in pug_data["link_requests"]:
            await interaction.response.send_message("This request was already handled.", ephemeral=True)
            return
        await interaction.response.send_modal(LinkDenyModal(self.rid))


class LinkDenyModal(discord.ui.Modal, title="Deny Link Request"):
    reason = discord.ui.TextInput(
        label="Reason (optional)", style=discord.TextStyle.paragraph, required=False, max_length=300
    )

    def __init__(self, rid: str):
        super().__init__()
        self.rid = rid

    async def on_submit(self, interaction: discord.Interaction):
        req = pug_data["link_requests"].get(self.rid)
        if not req:
            await interaction.response.send_message("This request was already handled.", ephemeral=True)
            return
        reason = self.reason.value.strip() or "No reason provided."
        # Ack first, then do the slow Gist save / edits.
        await interaction.response.send_message("Denied. The user has been notified.", ephemeral=True)
        pug_data["link_requests"].pop(self.rid, None)
        save_pug_data()

        ch = interaction.client.get_channel(int(req.get("review_channel_id", 0)))
        if ch:
            try:
                msg = await ch.fetch_message(int(req["review_message_id"]))
                embed = msg.embeds[0] if msg.embeds else discord.Embed()
                embed.color = 0xDA3633
                embed.add_field(name="Status", value=f"Denied by {interaction.user.mention}: {reason}", inline=False)
                view = LinkReviewView(self.rid)
                for item in view.children:
                    item.disabled = True
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                pass

        try:
            user = await interaction.client.fetch_user(int(req["user_id"]))
            await user.send(
                f"Your **{BRAND}** account link was **denied**.\nReason: {reason}\n"
                f"You can re-submit in the account-link channel."
            )
        except discord.HTTPException:
            pass
