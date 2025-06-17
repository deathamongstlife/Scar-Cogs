from .modmail import AdvancedModmail

async def setup(bot):
    await bot.add_cog(AdvancedModmail(bot))
