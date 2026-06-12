import discord

from pug.config import PUG_NA_ROLE_ID, PUG_EU_ROLE_ID


async def apply_region_role(guild: discord.Guild, member: discord.Member, region: str):
    """Give the member their NA/EU role and remove the other. Only NA and EU have roles;
    any other region (or none) removes both. No-op if the role IDs aren't configured."""
    if not guild or not member:
        return
    region = (region or "").upper()
    na = guild.get_role(PUG_NA_ROLE_ID) if PUG_NA_ROLE_ID else None
    eu = guild.get_role(PUG_EU_ROLE_ID) if PUG_EU_ROLE_ID else None

    want = na if region == "NA" else (eu if region == "EU" else None)
    to_remove = [r for r in (na, eu) if r and r in member.roles and r != want]

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="PUG region sync")
        if want and want not in member.roles:
            await member.add_roles(want, reason="PUG region sync")
    except discord.HTTPException:
        pass
