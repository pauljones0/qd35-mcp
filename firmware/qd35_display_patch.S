.syntax unified
.cpu cortex-m3
.thumb

/*
 * QD35 Cortex-M display-control patch for official lighting-MCU firmware 1.3.0.
 *
 * This is original patch code. It is linked at 0x0800BE20 in the unused gap
 * immediately before the device's animation-storage region (0x0800C000).
 * The patch is volatile-by-design: its state lives in unused SRAM and resets
 * to the stock all-visible/max-brightness behavior on every power cycle.
 */

/* The vector-table MSP is moved down by 24 bytes to reserve this storage. */
.equ PATCH_STATE,   0x20002718
.equ DISPLAY_BUF,   0x200016B8
.equ GLOBAL_STATE,  0x200013CC
.equ PARSE_HEX,     0x08002A4B
.equ WRITE_RAM,     0x08008F29
.equ DISPLAY_CTL_BODY, 0x08008FCF
.equ STOCK_LOW_INIT,   0x0800AB79
.equ STOCK_RESET_NEXT, 0x0800B2F9
.equ PARSER_RETURN, 0x080039C5

.equ MAGIC,         0xD5
.equ DEFAULT_FLAGS, 0x0F

.section .text.patch,"ax",%progbits
.global patch_start
.type patch_start,%function
.thumb_func
patch_start:

/* Replaces the effect-zero branch from +LED+SET. r4 is the ASCII command. */
.global private_command
.type private_command,%function
.thumb_func
private_command:
    push {r3-r7, lr}
    bl init_state
    mov r5, r0

    /* speed is a magic byte so accidental effect zero cannot enter the API */
    movs r0, #0x19
    bl parse_pair
    cmp r0, #0x35
    bne 9f

    movs r0, #0x11
    bl parse_pair
    mov r7, r0
    cmp r7, #0xd0
    beq 1f
    cmp r7, #0xd3
    beq 1f
    cmp r7, #0xd1
    beq 3f
    cmp r7, #0xd4
    beq .Lack
    cmp r7, #0xd2
    bne 9f
    movs r0, #0
    strb r0, [r5]
    bl init_state
    b 7f

/* D0 replaces all settings. D3 updates only fields selected by a mask. */
1:  movs r0, #0x13
    bl parse_pair
    and r0, r0, #0x3f
    mov r6, r0
    movs r0, #0x15
    bl parse_pair
    cmp r0, #7
    bhi 9f
    cmp r7, #0xd3
    beq .Lmasked
    strb r6, [r5, #1]
    strb r0, [r5, #2]
    b 7f
.Lmasked:
    mov r7, r0
    movs r0, #0x17
    bl parse_pair
    cmp r0, #0x7f
    bhi 9f
    tst r0, #0x40
    it ne
    strbne r7, [r5, #2]
    and r0, r0, #0x3f
    ldrb r1, [r5, #1]
    bics r1, r0
    ands r6, r0
    orrs r1, r6
    strb r1, [r5, #1]
    b 7f

/* D1: raw display-grid byte index in G and byte value in B. */
3:  movs r0, #0x13
    bl parse_pair
    cmp r0, #7
    bhi 9f
    mov r6, r0
    movs r0, #0x15
    bl parse_pair
    add r6, r5
    strb r0, [r6, #4]

7:  bl .Ldisplay_write
.Lack:
    /* Preserve the stock parser's ordinary SET_LIGHT acknowledgement path. */
    ldr r0, =GLOBAL_STATE
    movs r1, #1
    strb.w r1, [r0, #0x77]
9:  pop {r3-r7, lr}
    ldr pc, =PARSER_RETURN

.thumb_func
parse_pair:
    adds r2, r0, #1
    ldrb r1, [r4, r2]
    ldrb r0, [r4, r0]
    ldr r3, =PARSE_HEX
    bx r3

.type init_state,%function
.thumb_func
init_state:
    ldr r0, =PATCH_STATE
    ldrb r1, [r0]
    cmp r1, #MAGIC
    beq 2f
    ldr r1, =((7 << 16) | (DEFAULT_FLAGS << 8) | MAGIC)
    str r1, [r0]
    ldr r1, =DISPLAY_BUF
    ldm r1, {r2, r3}
    strd r2, r3, [r0, #4]
2:  bx lr

/* Replaces 0x08006874. Copies and masks the segment buffer, then writes it. */
.global display_write
.type display_write,%function
.thumb_func
display_write:
.Ldisplay_write:
    ldr r1, =DISPLAY_BUF
.global display_write_buffer
.thumb_func
display_write_buffer:
    mov r12, r1
    ldr r0, =GLOBAL_STATE
    ldrb r1, [r0, #4]
    cbz r1, .Lskip
    ldrb r1, [r0, #0x6d]
    cbz r1, .Ldraw
.Lskip:
    bx lr
.Ldraw:
    push {r3-r7, lr}
    mov r6, r12
    bl init_state
    mov r4, r0
    ldrb r5, [r4, #1]
    tst r5, #0x20
    it ne
    addne r6, r4, #4
    add r7, r4, #12
    movs r2, #0
1:  ldrb r0, [r6, r2]
    strb r0, [r7, r2]
    adds r2, #1
    cmp r2, #8
    blo 1b

    /* Mask byte zero separately, then use one mask for bytes 1–7. */
    ldrb r0, [r7]
    movs r1, #0xff
    tst r5, #2
    itt eq
    andeq r0, r0, #0xf0
    andeq r1, r1, #0xc0
    tst r5, #4
    itt eq
    andeq r0, r0, #0x9f
    andeq r1, r1, #0xbf
    tst r5, #8
    itt eq
    andeq r0, r0, #0x6f
    andeq r1, r1, #0x7f
    strb r0, [r7]
    movs r2, #1
2:  ldrb r0, [r7, r2]
    ands r0, r1
    strb r0, [r7, r2]
    adds r2, #1
    cmp r2, #8
    blo 2b

    tst r5, #1
    beq 8f
    movs r0, #0xc0
    mov r1, r7
    movs r2, #8
    ldr r3, =WRITE_RAM
    blx r3
8:  bl .Ldisplay_control
    pop {r3-r7, pc}

/* All stock display-on callers and the patched writer honor these settings. */
.global display_control
.thumb_func
display_control:
.Ldisplay_control:
    push {r4, lr}
    bl init_state
    mov r4, r0
    ldrb r2, [r4, #1]
    tst r2, #1
    beq 11f
    tst r2, #0x10
    beq 10f
    ldr r0, =GLOBAL_STATE
    ldrb r0, [r0, #0x12]
    cbz r0, 11f
    movs r1, #8
    muls r0, r1
    subs r0, #1
    movs r1, #100
    udiv r0, r0, r1
    b 12f
10: ldrb r0, [r4, #2]
12: adds r0, #0x88
    b 13f
11: movs r0, #0x80
13: mov r4, r0
    ldr r3, =DISPLAY_CTL_BODY
    bx r3

.global reset_entry
.thumb_func
reset_entry:
    ldr r0, =PATCH_STATE
    msr msp, r0
    movs r1, #0
    strb r1, [r0]
    ldr r3, =STOCK_LOW_INIT
    blx r3
    ldr r3, =STOCK_RESET_NEXT
    bx r3

.ltorg
.global patch_end
patch_end:
