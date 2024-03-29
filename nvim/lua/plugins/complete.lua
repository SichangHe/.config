U = require('util')

return {
    {
        'tzachar/cmp-tabnine',
        build = './install.sh',
        dependencies = 'hrsh7th/nvim-cmp',
    },

    {
        'zbirenbaum/copilot.lua',
        opts = {
            panel = {
                enabled = false,
            },
            suggestion = {
                enabled = false,
            },
            filetypes = {
                yaml = true,
                markdown = true,
            },
        },
        event = 'VeryLazy',
    },

    {
        'zbirenbaum/copilot-cmp',
        config = true,
        event = 'VeryLazy',
    },

    {
        'hrsh7th/nvim-cmp',
        dependencies = {
            'saadparwaiz1/cmp_luasnip',
            'zbirenbaum/copilot-cmp',
            'hrsh7th/cmp-nvim-lsp',
            'rafamadriz/friendly-snippets',
            'L3MON4D3/LuaSnip',
        },
        ---@param opts cmp.ConfigSchema
        opts = function(_, opts)
            local cmp = require('cmp')
            local snip = require('luasnip')
            require("luasnip.loaders.from_vscode").lazy_load {}
            require("luasnip.loaders.from_vscode").lazy_load {
                paths = { U.expand('~/.config/Code/User/snippets') },
            }
            table.insert(opts.snippet, 1, {
                expand = function(args)
                    snip.lsp_expand(args.body)
                end,
            })
            table.insert(opts.sources, 1, {
                { name = 'crates' },
                { name = 'nvim_lsp' },
                { name = 'vimtex' },
                { name = 'luasnip' },
                { name = 'buffer' },
                { name = 'copilot' },
                { name = 'cmp_tabnine' },
            })
            opts.mapping = cmp.mapping.preset.insert {
                ['<Tab>'] = cmp.mapping(function(fallback)
                    if cmp.visible() then
                        U.break_undo()
                        cmp.confirm {
                            behavior = cmp.ConfirmBehavior.Replace,
                            select = true,
                        }
                    else
                        fallback()
                    end
                end, { 'i', 's' }),
                ['<C-j>'] = cmp.mapping(function()
                    if cmp.visible() then
                        cmp.select_next_item()
                    elseif snip.expand_or_jumpable() then
                        snip.expand_or_jump()
                    else
                        cmp.complete {}
                    end
                end, { 'i', 's' }),
                ['<C-k>'] = cmp.mapping(function(fallback)
                    if cmp.visible() then
                        cmp.select_prev_item()
                    elseif snip.jumpable(-1) then
                        snip.jump(-1)
                    else
                        fallback()
                    end
                end, { 'i', 's' }),
                ["<C-b>"] = cmp.mapping.scroll_docs(-12),
                ["<C-f>"] = cmp.mapping.scroll_docs(12),
            }
        end,
    }
}
