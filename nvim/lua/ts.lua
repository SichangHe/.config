return {
    {
        'nvim-treesitter/nvim-treesitter',
        build = ':TSUpdate',
        dependencies = {
            'RRethy/nvim-treesitter-endwise',
            'windwp/nvim-ts-autotag',
            'mrjones2014/nvim-ts-rainbow',
        },
        config = function()
            local function disable(lang, bufnr)
                -- Prevent stuck by large file or single line huge file.
                return U.b.large_buf or U.api.nvim_buf_line_count(bufnr) < 2
            end
            require('nvim-treesitter.configs').setup {
                highlight = {
                    enable = true,
                    disable = disable,
                },
                incremental_selection = {
                    enable = true,
                    disable = disable,
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
                    'vim',
                },
                auto_install = true,
                rainbow = {
                    enable = true,
                    disable = disable,
                },
                autotag = {
                    enable = true,
                    disable = disable,
                },
                endwise = {
                    enable = true,
                    disable = disable,
                },
            }
        end,
    },

    { 'mrjones2014/nvim-ts-rainbow' },
}
