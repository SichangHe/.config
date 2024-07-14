#!/usr/bin/env python3
def lua_repr(s: str) -> str:
    ordinary_repr = f"'{s}'"
    return ordinary_repr if repr(s) == ordinary_repr else f"[[{s}]]"


def main():
    # Note: keep these aligned.
    original = r"""`1234567890-=qwertyuiop[]\asdfghjkl;'zxcvbnm,./"""
    w_option = r"""`¡™£¢∞§¶•ªº–≠œ∑´®†¥¨ˆøπ“‘«åß∂ƒ©˙∆˚¬…æΩ≈ç√∫˜µ≤≥÷"""
    w_sh_opt = r"""`⁄€‹›ﬁﬂ‡°·‚—±Œ„´‰ˇÁ¨ˆØ∏”’»ÅÍÎÏ˝ÓÔÒÚÆ¸˛Ç◊ı˜Â¯˘¿"""
    print("local mapping = {")
    for o, w, s in zip(original, w_option, w_sh_opt):
        print(f"{{{lua_repr(o)}, {lua_repr(w)}, {lua_repr(s)}}},")
    print("}")


main() if __name__ == "__main__" else None
