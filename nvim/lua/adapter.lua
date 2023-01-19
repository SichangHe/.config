return function(use)
    use {
        "jayp0521/mason-nvim-dap.nvim",
        requires = {
            "williamboman/mason.nvim",
            "mfussenegger/nvim-dap",
        },
        config = function()
            require("mason-nvim-dap").setup({
                ensure_installed = { 'codelldb', 'debugpy' }
            })
        end
    }
end
