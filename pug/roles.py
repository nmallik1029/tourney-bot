import discord

from core.guild_config import pug_na_role_id, pug_eu_role_id


async def apply_region_role(guild: discord.Guild, member: discord.Member, region: str):
    """Give the member their NA/EU role and remove the other. Only NA and EU have roles;
    any other region (or none) removes both. No-op if the role IDs aren't configured."""
    if not guild or not member:
        return
    region = (region or "").upper()
    na_id = pug_na_role_id(guild.id)
    eu_id = pug_eu_role_id(guild.id)
    na = guild.get_role(na_id) if na_id else None
    eu = guild.get_role(eu_id) if eu_id else None

    want = na if region == "NA" else (eu if region == "EU" else None)
    to_remove = [r for r in (na, eu) if r and r in member.roles and r != want]

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="PUG region sync")
        if want and want not in member.roles:
            await member.add_roles(want, reason="PUG region sync")
    except discord.HTTPException:
        pass
