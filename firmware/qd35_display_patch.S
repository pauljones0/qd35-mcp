.syntax unified
.cpu cortex-m3
.thumb

/*
 * QD35 STM32 display-control patch for official lighting-MCU firmware 1.3.0.
 *
 * This is original patch code. It is linked at 0x0800BE20 in the unused gap
 * immediately before the device's animation-storage region (0x0800C000).
 * The patch is volatile-by-design: its state lives in unused SRAM and resets
 * to the stock all-visible/max-brightness behavior on every power cycle.
 */

.equ PATCH_STATE,   0x20001730
.equ DISPLAY_BUF,   0x200016B8
.equ GLOBAL_STATE,  0x200013CC
.equ PARSE_HEX,     0x08002A4B
.equ WRITE_RAM,     0x08008F29
.equ DISPLAY_CTL,   0x08008FCB
.equ DISPLAY_OFF,   0x08008FEF
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
    push {r4-r7, lr}
    bl init_state
    mov r5, r0

    /* speed is a magic byte so accidental effect zero cannot enter the API */
    ldrb r0, [r4, #0x19]
    ldrb r1, [r4, #0x1a]
    ldr r3, =PARSE_HEX
    blx r3
    cmp r0, #0x35
    bne 9f

    ldrb r0, [r4, #0x11]
    ldrb r1, [r4, #0x12]
    ldr r3, =PARSE_HEX
    blx r3
    mov r6, r0
    cmp r6, #0xd0
    beq 1f
    cmp r6, #0xd1
    beq 3f
    cmp r6, #0xd2
    bne 9f
    movs r0, #0
    strb r0, [r5]
    bl init_state
    b 7f

/* D0: flags in G and manual CMS1640 brightness (0..7) in B. */
1:  ldrb r0, [r4, #0x13]
    ldrb r1, [r4, #0x14]
    ldr r3, =PARSE_HEX
    blx r3
    and r0, r0, #0x3f
    mov r6, r0
    ldrb r0, [r4, #0x15]
    ldrb r1, [r4, #0x16]
    ldr r3, =PARSE_HEX
    blx r3
    cmp r0, #7
    bhi 9f
    /* Commit only after every field has validated. */
    strb r6, [r5, #1]
    strb r0, [r5, #2]
    b 7f

/* D1: raw display-grid byte index in G and byte value in B. */
3:  ldrb r0, [r4, #0x13]
    ldrb r1, [r4, #0x14]
    ldr r3, =PARSE_HEX
    blx r3
    cmp r0, #7
    bhi 9f
    mov r6, r0
    ldrb r0, [r4, #0x15]
    ldrb r1, [r4, #0x16]
    ldr r3, =PARSE_HEX
    blx r3
    add r6, r5
    strb r0, [r6, #4]

7:  bl .Ldisplay_write
    /* Preserve the stock parser's ordinary SET_LIGHT acknowledgement path. */
    ldr r0, =GLOBAL_STATE
    movs r1, #1
    strb.w r1, [r0, #0x77]
9:  pop {r4-r7, lr}
    ldr pc, =PARSER_RETURN

.type init_state,%function
.thumb_func
init_state:
    ldr r0, =PATCH_STATE
    ldrb r1, [r0]
    cmp r1, #MAGIC
    beq 2f
    movs r1, #MAGIC
    strb r1, [r0]
    movs r1, #DEFAULT_FLAGS
    strb r1, [r0, #1]
    movs r1, #7
    strb r1, [r0, #2]
    movs r1, #0xff
    strb r1, [r0, #3]
    ldr r1, =DISPLAY_BUF
    movs r2, #0
1:  ldrb r3, [r1, r2]
    add r12, r0, r2
    strb.w r3, [r12, #4]
    adds r2, #1
    cmp r2, #8
    blo 1b
2:  bx lr

/* Replaces 0x08006874. Copies and masks the segment buffer, then writes it. */
.global display_write
.type display_write,%function
.thumb_func
display_write:
.Ldisplay_write:
    push {r4-r7, lr}
    bl init_state
    mov r4, r0
    ldrb r5, [r4, #1]
    tst r5, #0x20
    ite ne
    addne r6, r4, #4
    ldreq r6, =DISPLAY_BUF
    add r7, r4, #12
    movs r2, #0
1:  ldrb r0, [r6, r2]
    strb r0, [r7, r2]
    adds r2, #1
    cmp r2, #8
    blo 1b

    /* Clock digits. */
    tst r5, #2
    bne 3f
    ldrb r0, [r7]
    and r0, r0, #0xf0
    strb r0, [r7]
    movs r2, #1
2:  ldrb r0, [r7, r2]
    and r0, r0, #0xc0
    strb r0, [r7, r2]
    adds r2, #1
    cmp r2, #8
    blo 2b

    /* EDIFIER logo grid. */
3:  tst r5, #4
    bne 5f
    ldrb r0, [r7]
    and r0, r0, #0x9f
    strb r0, [r7]
    movs r2, #1
4:  ldrb r0, [r7, r2]
    and r0, r0, #0xbf
    strb r0, [r7, r2]
    adds r2, #1
    cmp r2, #8
    blo 4b

    /* Remaining icon/symbol grid bits. */
5:  tst r5, #8
    bne 7f
    movs r2, #0
6:  ldrb r0, [r7, r2]
    and r0, r0, #0x7f
    strb r0, [r7, r2]
    adds r2, #1
    cmp r2, #8
    blo 6b

    /* With both non-clock groups disabled, byte zero must also be empty.
     * Its low five bits are not clock digits and were missed by the original
     * semantic masks. This guarantees a digits-only electrical framebuffer;
     * the physical wordmark still catches light from illuminated digits. */
    tst r5, #4
    bne 7f
    movs r0, #0
    strb r0, [r7]

7:  tst r5, #1
    bne 8f
    ldr r3, =DISPLAY_OFF
    blx r3
    pop {r4-r7, pc}

8:  movs r0, #0xc0
    mov r1, r7
    movs r2, #8
    ldr r3, =WRITE_RAM
    blx r3
    tst r5, #0x10
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
    ldr r3, =DISPLAY_CTL
    blx r3
    pop {r4-r7, pc}
11: ldr r3, =DISPLAY_OFF
    blx r3
    pop {r4-r7, pc}

.ltorg
.global patch_end
patch_end:
