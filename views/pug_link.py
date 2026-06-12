import io
import uuid

import discord

from pug.config import VALID_REGIONS, BRAND, member_is_pug_staff
from pug.storage import (
    pug_data,
    pending_link_threads,
    save_pug_data,
    add_username,
    set_primary_username,
    set_region,
    username_to_discord,
)


# ── #account-link entry point ──────────────────────────────────────────────────
def build_account_link_embed() -> discord.Embed:
    embed = discord.Embed(
        title=f"Link Your {BRAND} Account",
        description=(
            "To play PUGs you must link your Krunker account.\n\n"
            "Click **Link Your Account** below and provide:\n"
            "**1.** Your Krunker **username(s)** (and which is your **primary**)\n"
            "**2.** A **screenshot** proving the account is yours (username visible in Krunker)\n"
            "**3.** Your **region** (NA / EU / ASIA / OCE / ME)\n\n"
            "You'll get a private thread to drop your screenshot in. An admin then reviews "
            "and approves your request."
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
    usernames = discord.ui.TextInput(
        label="Krunker Username(s)", placeholder="Comma-separated if you have multiple", required=True, max_length=200
    )
    primary = discord.ui.TextInput(
        label="Primary Username", placeholder="Your main account name", required=True, max_length=60
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

        names = [u.strip() for u in self.usernames.value.split(",") if u.strip()]
        if not names:
            await interaction.response.send_message("Enter at least one username.", ephemeral=True)
            return
        primary = self.primary.value.strip()
        if primary.lower() not in [n.lower() for n in names]:
            names.insert(0, primary)

        # Private thread for the screenshot upload
        try:
            thread = await interaction.channel.create_thread(
                name=f"link-{interaction.user.name}"[:90],
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
            await thread.add_user(interaction.user)
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Couldn't create your upload thread ({e}). Make sure the bot can create private threads here.",
                ephemeral=True,
            )
            return

        pending_link_threads[thread.id] = {
            "user_id": interaction.user.id,
            "usernames": names,
            "primary": primary,
            "region": region,
        }
        await thread.send(
            f"<@{interaction.user.id}> Upload your **Krunker screenshot** here (your username must be visible) "
            f"to finish. Just drag or paste the image into this thread.",
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        await interaction.response.send_message(
            f"Almost done — upload your screenshot in {thread.mention} to submit your request.", ephemeral=True
        )


# ── Admin review ────────────────────────────────────────────────────────────────
async def post_link_request(bot, user_id: int, usernames: list[str], primary: str, region: str,
                            image_bytes: bytes, filename: str) -> str | None:
    """Post a review embed (with the proof screenshot) to the review channel."""
    review_ch = bot.get_channel(pug_data["config"].get("review_channel_id"))
    if not review_ch:
        return None

    rid = uuid.uuid4().hex[:8]
    embed = discord.Embed(title="Account Link Request", color=0xFFA500)
    embed.add_field(name="User", value=f"<@{user_id}>", inline=False)
    embed.add_field(name="Username(s)", value=", ".join(usernames), inline=True)
    embed.add_field(name="Primary", value=primary, inline=True)
    embed.add_field(name="Region", value=region, inline=True)
    embed.set_image(url=f"attachment://{filename}")

    file = discord.File(io.BytesIO(image_bytes), filename=filename)
    msg = await review_ch.send(embed=embed, file=file, view=LinkReviewView(rid))

    pug_data["link_requests"][rid] = {
        "user_id": user_id,
        "usernames": usernames,
        "primary": primary,
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

        uid = int(req["user_id"])
        applied, skipped = [], []
        for u in req["usernames"]:
            owner = username_to_discord(u)
            if owner is not None and owner != uid:
                skipped.append(u)
                continue
            add_username(uid, u)
            applied.append(u)
        if req.get("primary"):
            set_primary_username(uid, req["primary"])
        set_region(uid, req["region"])
        pug_data["link_requests"].pop(self.rid, None)
        save_pug_data()

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = 0x3FB950
        status = f"Accepted by {interaction.user.mention}"
        if skipped:
            status += f"\nSkipped (already linked to someone else): {', '.join(skipped)}"
        embed.add_field(name="Status", value=status, inline=False)
        for item in self.view.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self.view)

        try:
            user = await interaction.client.fetch_user(uid)
            await user.send(
                f"Your **{BRAND}** account link was **approved**! "
                f"Linked: {', '.join(applied) or 'none'} ({req['region']}). You can now queue."
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
        pug_data["link_requests"].pop(self.rid, None)
        save_pug_data()
        await interaction.response.send_message("Denied. The user has been notified.", ephemeral=True)

        # Edit the original review message
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
