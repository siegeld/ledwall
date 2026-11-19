MEMORY {
	rom : ORIGIN = 0x00000000, LENGTH = 0x00010000
	sram : ORIGIN = 0x10000000, LENGTH = 0x00002000
	spiflash : ORIGIN = 0x80000000, LENGTH = 0x00200000
	main_ram : ORIGIN = 0x40000000, LENGTH = 0x00400000
	csr : ORIGIN = 0xf0000000, LENGTH = 0x00010000
}

/* Manually modified, do not change */
/* REGION_ALIAS("REGION_TEXT", main_ram); */
/* REGION_ALIAS("REGION_RODATA", main_ram); */
/* REGION_ALIAS("REGION_DATA", main_ram); */
/* REGION_ALIAS("REGION_BSS", main_ram); */
/* REGION_ALIAS("REGION_HEAP", main_ram); */
/* REGION_ALIAS("REGION_STACK", main_ram); */

/* SECTIONS */
/* { */
/* 	.main_ram (NOLOAD) : ALIGN(4) */
/* 	{ */
/* 		*(.main_ram .main_ram.*); */
/* 		. = ALIGN(4); */
/* 	} > main_ram */
/* } INSERT AFTER .bss; */

/* CPU reset location. */
_stext = 0x000000;
