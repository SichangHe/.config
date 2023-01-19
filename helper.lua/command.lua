local M = {}

function M.run(command, pattern)
    local handle = io.popen(command)
    local result = handle:read(pattern or "*a")
    handle:close()
    return result
end

return M
