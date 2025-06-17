from .modmail import ModMail

async def setup(bot):
    await bot.add_cog(ModMail(bot))
