return function(use)
    use {
        'nvim-treesitter/nvim-treesitter',
        run = ':TSUpdate',
        requires = {
            'RRethy/nvim-treesitter-endwise',
            'windwp/nvim-ts-autotag',
            'mrjones2014/nvim-ts-rainbow',
        },
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
                    'elixir',
                    'erlang',
                    'fish',
                    'heex',
                    'javascript',
                    'jsonc',
                    'julia',
                    'latex',
                    'lua',
                    'markdown',
                    'markdown_inline',
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
                endwise = {
                    enable = true,
                },
            }
        end,
    }

    use {
        'petertriho/nvim-scrollbar',
        config = function()
            require('scrollbar').setup()
        end,
    }

    use { 'mrjones2014/nvim-ts-rainbow' }
end
