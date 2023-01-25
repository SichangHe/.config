return function(use)
    use {
        'nvim-treesitter/nvim-treesitter',
        run = ':TSUpdate',
        requires = { 'windwp/nvim-ts-autotag', 'mrjones2014/nvim-ts-rainbow' },
        config = function()
            require('nvim-treesitter.configs').setup {
                highlight = {
                    enable = true,
                },
                incremental_selection = {
                    enable = true,
                    keymaps = {
                        init_selection = 'gnn',
                        node_incremental = 'grn',
                        scope_incremental = 'grc',
                        node_decremental = 'grm',
                    },
                },
                ensure_installed = {
                    'bash',
                    'c',
                    'fish',
                    'javascript',
                    'jsonc',
                    'julia',
                    'latex',
                    'lua',
                    'markdown',
                    'python',
                    'ruby',
                    'rust',
                    'typescript',
                },
                auto_install = true,
                rainbow = {
                    enable = true,
                },
                autotag = {
                    enable = true,
                },
            }
        end,
    }

    use {
        'windwp/nvim-ts-autotag',
    }

    use {
        'petertriho/nvim-scrollbar',
        config = function()
            require('scrollbar').setup()
        end,
    }

    use { 'mrjones2014/nvim-ts-rainbow' }
end
