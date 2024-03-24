U = require('util')

local function unset_selected_lazyvim_keymaps()
    for _, key in pairs({
        '<S-h>',
        '<S-L>',
    }) do
        pcall(U.del_key, 'n', key)
    end
end


unset_selected_lazyvim_keymaps()
