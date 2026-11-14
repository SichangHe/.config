return {
	-- Email through the `himalaya` CLI v1 (not v2); reads `~/.config/himalaya/config.toml`.
	{
		"pimalaya/himalaya-vim",
		cmd = "Himalaya",
		keys = { { "<leader>oe", "<cmd>Himalaya<cr>", desc = "Email inbox" } },
	},

	-- Omnigent agents; needs the Omnigent server on the same machine.
	{
		"SichangHe/nvim_omnigent",
		event = "VeryLazy",
		opts = {},
	},

	-- Zulip conversations; reads the account from `~/.zuliprc`.
	{
		"SichangHe/nvim_zulip",
		cmd = "Zulip",
		keys = { { "<leader>oz", desc = "Zulip conversations" } },
		opts = {},
	},

	-- Inline images in buffers, scaled to the window width.
	{
		"SichangHe/nvim_inline_image",
		dir = "/ssd1/sichangheagent/nvim_inline_image",
		cmd = "InlineImage",
		keys = { { "<leader>uP", function() require("inline_image").toggle() end, desc = "Toggle inline images" } },
		opts = {},
	},
}
