U = require("util")

-- U.lsp.set_log_level('debug') -- debug LSP

local servers = {
	basedpyright = {
		settings = {
			basedpyright = {
				typeCheckingMode = "basic",
			},
		},
	},
	bashls = {},
	clangd = {},
	cssls = {},
	elixirls = {},
	emmet_ls = {},
	gleam = {
		mason = false,
	},
	html = {},
	jsonls = {},
	julials = {},
	ltex = {
		autostart = false, -- LTeX is too heavy for regular use.
		settings = {
			ltex = {
				dictionary = {
					["en-US"] = {}, -- Initialized below.
				},
			},
		},
		on_init = function(client, bufnr)
			_ = bufnr
			local spell_file_name = U.conf_loc .. "spell/en.utf-8.add"
			local spell_file = io.open(spell_file_name, "r")
			if spell_file then
				local dict = client.config.settings.ltex.dictionary["en-US"]
				for line in spell_file:lines() do
					table.insert(dict, line)
				end
				spell_file:close()
			end
		end,
	},
	pylsp = {
		settings = {
			pylsp = {
				configurationSources = { "mypy" },
				plugins = {
					autopep8 = { enabled = false },
					jedi_completion = {
						eager = true,
						fuzzy = true,
					},
					-- Use BasedPyright.
					jedi_definition = { enabled = false },
					jedi_references = { enabled = false },
					mccabe = { enabled = false },
					mypy = {
						enabled = true,
						report_progress = true,
					},
					pycodestyle = { enabled = false },
					pyflakes = { enabled = false },
					yapf = { enabled = false },
				},
			},
		},
	},
	ruff = {
		on_attach = function(client, bufnr_attached)
			_ = client
			-- Ruff automatic import organization.
			LazyVim.format.register({
				name = "ruff.organize_imports",
				priority = 50, -- Smaller than Conform's 100.
				primary = false, -- Conform is primary.
				format = function(bufnr)
					if bufnr == bufnr_attached then
						vim.lsp.buf.code_action({
							context = {
								only = { "source.organizeImports" },
								diagnostics = {},
							},
							apply = true,
						})
					end
				end,
				sources = function(_)
					return { "ruff.organize_imports" } -- Dummy name.
				end,
			})
		end,
	},
	solargraph = {},
	sourcekit = {
		mason = false,
	},
	svelte = {},
	tailwindcss = {},
	taplo = {},
	zls = {},
}

local function register_mdbook_ls()
	local function execute_command_with_params(params)
		local clients = vim.lsp.get_clients({
			bufnr = vim.api.nvim_get_current_buf(),
			name = "mdbook_ls",
		})
		for _, client in ipairs(clients) do
			client:request("workspace/executeCommand", params, nil, 0)
		end
	end
	local function open_preview()
		local params = {
			command = "open_preview",
			arguments = { "127.0.0.1:33000", vim.api.nvim_buf_get_name(0) },
		}
		execute_command_with_params(params)
	end
	local function stop_preview()
		local params = {
			command = "stop_preview",
			arguments = {},
		}
		execute_command_with_params(params)
	end

	vim.lsp.config.mdbook_ls = {
		cmd = { "mdbook-ls" },
		filetypes = { "markdown" },
		root_markers = { "book.toml" },
		docs = {
			description = [[The mdBook Language Server for previewing mdBook projects live.]],
		},
	}

	vim.api.nvim_create_user_command("MDBookLSOpenPreview", open_preview, {
		desc = "Open mdBook-LS preview",
	})

	vim.api.nvim_create_user_command("MDBookLSStopPreview", stop_preview, {
		desc = "Stop mdBook-LS preview",
	})
end

local function register_natural_syntax_ls()
	vim.lsp.config.natural_syntax_ls = {
		cmd = {
			U.fn.expand("~/.config/helper.sh/natural-syntax-ls.sh"),
		},
		filetypes = { "tex", "markdown", "text" },
		single_file_support = true,
		init_options = {
			token_map_update = {
				CC = { type = "comment" },
				DT = { type = "comment" },
				IN = { type = "comment" },
				PDT = { type = "comment" },
				TO = { type = "comment" },
				UH = { type = "comment" },
				NN = vim.NIL,
				NNS = vim.NIL,
				VB = vim.NIL,
				VBD = vim.NIL,
				VBG = vim.NIL,
				VBN = vim.NIL,
				VBP = vim.NIL,
				VBZ = vim.NIL,
			},
		},
	}
end

local markdownlint_cli2_args = {
	"--config",
	U.expand("~/.config/.markdownlint-cli2.jsonc"),
}

return {
	{
		"stevearc/conform.nvim",
		opts = {
			formatters = {
				["bibtex-tidy"] = {
					prepend_args = {
						"--curly",
						"--numeric",
						"--months",
						"--tab",
						"--no-align",
						"--blank-lines",
						"--sort=special,year,month,key",
						"--drop-all-caps",
						"--no-escape",
						"--sort-fields",
						"--trailing-commas",
					},
				},
				fmtm = {
					command = "fmtm",
				},
				fmtt_latex = {
					command = "fmtt",
					args = { "-l" },
				},
				latexindent = {
					prepend_args = {
						"-l",
						U.expand("~/.config/latexindent_config.yaml"),
						"-m",
					},
				},
				markdownlint_cli2 = {
					prepend_args = markdownlint_cli2_args,
				},
				shfmt = {
					prepend_args = { "-i", "4", "-bn", "-ci", "-sr" },
				},
			},
			formatters_by_ft = {
				bib = { "bibtex-tidy" },
				css = { "prettierd" },
				markdown = { "fmtm" },
				python = { "ruff_format" },
				quarto = { "markdownlint-cli2", "fmtm" },
				javascript = { "prettierd" },
				sh = { "shfmt" },
				tex = { "fmtt_latex", "latexindent", "fmtt_latex" },
				-- Prettierd
				handlebars = { "prettierd" },
				yaml = { "prettierd" },
			},
		},
	},

	{
		"akinsho/flutter-tools.nvim",
		ft = { "dart" },
		dependencies = { "stevearc/dressing.nvim", "nvim-lua/plenary.nvim" },
		config = true,
	},

	{
		"ray-x/go.nvim",
		dependencies = {
			"ray-x/guihua.lua",
			"neovim/nvim-lspconfig",
			"nvim-treesitter/nvim-treesitter",
		},
		ft = { "go", "gomod" },
		opts = {
			lsp_cfg = true,
		},
		build = ':lua require("go.install").update_all_sync()',
	},

	{
		"ionide/Ionide-vim",
		dependencies = { "neovim/nvim-lspconfig" },
		ft = { "fsharp" },
	},

	{
		"glepnir/lspsaga.nvim",
		event = "VeryLazy",
		dependencies = { "nvim-tree/nvim-web-devicons" },
		opts = {
			lightbulb = {
				enable_in_insert = false,
				virtual_text = false,
			},
			symbol_in_winbar = {
				enable = false,
			},
			ui = {
				border = "none",
			},
		},
	},

	{
		"neovim/nvim-lspconfig",
		opts = function(_, opts)
			register_mdbook_ls()
			register_natural_syntax_ls()
			opts.diagnostics = U.deep_extend(opts.diagnostics, {
				virtual_text = {
					spacing = 1,
					source = false,
				},
			})
			opts.servers = U.deep_extend(opts.servers, servers)
			opts.setup = U.deep_extend(opts.setup, {
				rust_analyzer = function() -- Prevent double setup.
					return true
				end,
				pyright = function() -- Disable Pyright.
					return true
				end,
			})
		end,
	},

	{
		"williamboman/mason.nvim",
		opts = function(_, opts)
			-- Override and not to install with Mason.
			local to_remove = {
				stylua = true,
			}
			for index, program in ipairs(opts.ensure_installed) do
				if to_remove[program] then
					table.remove(opts.ensure_installed, index)
				end
			end
			-- Install these in addition.
			vim.list_extend(opts, {
				"bibtex-tidy",
				"prettierd",
			})
		end,
	},

	{
		"mfussenegger/nvim-lint",
		opts = function(_, _)
			local markdownlint_cli2 = require("lint").linters["markdownlint-cli2"]
			markdownlint_cli2.args = markdownlint_cli2_args
		end,
	},
}
