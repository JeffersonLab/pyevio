
## Evio Version 6

A big factor for introducing yet another evio version was the desire to compress data in each block (now called a record). The HIPO format, in use in Jefferson Lab’s Hall B, already had the capability to compress data. In order to avoid proliferation of data formats, HIPO was merged with evio along with much of the code to handle data compression. This has added a great deal of complexity to the file and record headers as one can see following.

**FILE FORMAT**

Below is a diagram of how an HIPO/evio6 file is laid out. Each file written by the evio library has its own file header which appears once, at the very beginning of the file. It is always uncompressed. The raw files the JLab SRO hardware writes have no file header; see "SRO hardware layout" below. That is followed by an optional, uncompressed index array which contains pairs of 4-byte integers. Each pair, one pair per record, is the length of a record in bytes followed by the event count in that record. Generally, the index array is not used with evio files.

That, in turn, is followed by an optional, uncompressed user header which is just an array of data to be defined by the user. This array is padded to a 4-byte boundary. To be more specific, it holds user data in HIPO files, but in evio files the user header will only store any dictionary or first event provided to the file writer. The dictionary and first event are placed into a record and written out as the user header. More on records later. Finally, any index array and user header is followed by any number of records including the last record which may be a trailer. More on the trailer later.

![](data:image/jpeg;base64...)

**FILE HEADER**

First, let’s look at the file header (unless specified, each row signifies 32 bits):

MSB(31)                           LSB(0)
<-------------------- 32 bits ------------------>

|  |
| --- |
| **File Type ID** |
| **File Number** |
| **Header Length** |
| **Record Count** |
| **Index Array Length** |
| **Bit info       | Version** |
| **User Header Length** |
| **Magic Number** |
| **User Register, 64 bits** |
| **Trailer Position, 64 bits** |
| **User Integer 1** |
| **User Integer 2** |

1. For a HIPO file, the file type ID is 0x43455248, for an Evio file, it’s 0x4556494F. This is asci for HIPO and EVIO respectively.
2. The split number is used when splitting files during writing.
3. The header length is the number of 32 bit words in this header - set to 14 by default. Even though, theoretically, it can be changed, there are no means to do so through the evio library.
4. The record count is the number of records in this file.
5. The optional index array, which follows this file header, contains all record lengths in order. Each length is an unsigned 4-byte integer in units of bytes. This file header entry is the length of the index array in bytes. For evio files this will be 0 as the index array is not used in any evio library.
6. The version is the current evio format version number (6) and takes up the lowest 8 bits. The bit info word is used to store the various useful data listed in the table below.
7. The user “header” will hold any dictionary and first event provided to the file writer. This file header entry is the length of the user header in bytes.
8. The magic number is the value 0xc0da0100 and is used to check endianness.
9. The user register is a 64-bit register available to the user. This is not used by evio and always set to 0.
10. The trailer position is a 64-bit integer containing the byte offset from the start of the file to the trailer (which is a type of record). This may be 0 if the position was not available when originally writing or if there is no trailer.
11. The user integer #1 is just that, a 32-bit integer available to the user. This is not used by evio and always set to 0.
12. The user integer #2 is just that, a 32-bit integer available to the user. This is not used by evio and always set to 0.

**FILE HEADER’S BIT INFO WORD**

|  |  |
| --- | --- |
| **Bit # (0 = LSB)** | **Function** |
| 0-7 | Evio Version # = 6 |
| 8 | = 1 if dictionary is included |
| 9 | = 1 if file has a “first” event – an event which gets included with every split file |
| 10 | = 1 if file has trailer containing an index array of record lengths |
| 11-19 | reserved |
| 20-21 | pad 1 |
| 22-23 | reserved |
| 24-25 | reserved |
| 26-27 | reserved |
| 28-31 | What type of header is this?  1 = Evio file header,  2 = Evio extended file header,  5 = HIPO file header,  6 = HIPO extended file header |

A note on the “evio extended file” header mentioned in the bitinfo table immediately above. The idea is to extend the header by adding extra words to its end. This has never been implemented. If the header was to be extended, the extra words would be user-defined integers. The third word, the header length, would need to be changed from its normal value of 14 to include the extra words.

**RECORD**

After the file header, index array, and user header, the file is divided into **records**. Following is the layout of a record:

**![](data:image/jpeg;base64...)**

Each record has a header. The header is followed by an index of event lengths – one word per length in the unit of bytes. The evio library always writes this index. Some writers omit it (index array length 0, as the SRO hardware does); pyevio then walks the events by the length word of each event bank, bounded by the record's event count and data end.

It is followed by an optional user header which functions in the same manner as the one in the file layout. It is an array of data defined by the user. This array is padded to a 4-byte boundary. For the evio format, if writing to a file, any dictionary and/or first event are exclusively placed in the file’s user header and nothing is ever placed in a record’s user header. If writing to a buffer, any dictionary and/or first event are exclusively placed in the user header in the format of a record (in other words, a record within a record). The EventWriter class, when writing to a buffer only writes one record and so that will contain the dictionary/first event. If using the Writer class, it’s data format agnostic and any user data may be written with any record as part of the user header.

Any index array and user header is followed by any number of events. These events look differently depending on whether you have evio or HIPO format. An evio event always ends on a 4-byte boundary which means that pad2 will always be 0. In the HIPO world, events can be of any size. See the graphic below.

If data compression is being used, the record header will not be compressed allowing a quick scan of the file without any decompression needing to take place. When reading, data is decompressed record by record as needed to access particular events.

**![](data:image/jpeg;base64...)**

**RECORD HEADER**

The fields of the record header are seen below (unless specified, each row signifies 32 bits):

MSB(31)                           LSB(0)
<-------------------- 32 bits ------------------>

|  |
| --- |
| **Record Length** |
| **Record Number** |
| **Header Length** |
| **Event Count** |
| **Index Array Length** |
| **Bit info       | Version** |
| **User Header Length** |
| **Magic Number** |
| **Uncompressed Data Length** |
| **CT | Compressed Data Length** |
| **User Register 1, 64 bits** |
| **User Register 2, 64 bits** |

1. The record length is number of 32 bit words in the record (including itself). In general, this will vary from record to record.
2. The record number is an id # used by the event writer. Can be used by reader to ensure data is received in the proper order.
3. The header length is the number of 32 bit words in this header - set to 14 by default. The evio library never writes another value. The SRO hardware writes 8-word headers that stop after the magic number (see "SRO hardware layout"); pyevio reads both and treats words 8—13 of an 8-word header as 0.
4. The event count is the number of events in this record - always integral.
5. The index array length is the length, in bytes, of the following index of event lengths.
6. The version is the current evio format version number (6) and takes up the lowest 8 bits. The bit info word is used to store the various useful data listed in the table below.
7. The user “header” is just a user-defined array which may contain anything. This record header entry is the length of the user header in bytes. If writing to a buffer and if there is a dictionary / first event, they will be stored in the user header and the length of that data will be stored here. If writing to a file, there is no user header data in a record so this entry will be 0.
8. The magic number is the value 0xc0da0100 and is used to check endianness.
9. The uncompressed data length is the length, in bytes, of the events alone. The index array and the user header are not included: the evio C++ and Java writers store the event bytes here in every record, and pyevio sizes the decompressed buffer as index array length + padded user header length + this word.
10. The highest 4 bits signify the type of compression used in the record (see table below). The other bits contain the padded length of the compressed data in 32-bit words; pad 3 (bits 24—25 of the bit info word) counts the filler bytes in the last word that are not part of the compressed stream. The compressed data starts right after the header; LZ4 and LZ4_BEST use the LZ4 block format (no frame header).
11. User register #1, 64 bits.
12. User register #2, 64 bits.

**TYPE OF COMPRESSION**

|  |  |
| --- | --- |
| **Value** | **Compression Type** |
| 0 | No compression |
| 1 | LZ4, fast |
| 2 | LZ4, best compression |
| 3 | gzip |

**RECORD HEADER’S BIT INFO WORD**

|  |  |
| --- | --- |
| **Bit # (0 = LSB)** | **Function** |
| 0-7 | Evio Version # = 6 |
| 8 | = 1 if dictionary is included (first record only) |
| 9 | = 1 if is last record in stream or file |
| 10 - 13 | type of events for CODA online in record:  ROC Raw = 0,  Physics = 1,  Partial Physics = 2,  Disentangled Physics = 3,  User = 4,  Control = 5,  Mixed = 6,  ROC Raw Streaming = 8,  Physics Streaming = 9,  Other = 15 |
| 14 | = 1 if this record has a “first event” (first record only, in every split file) |
| 15-19 | reserved |
| 20-21 | pad 1 |
| 22-23 | pad 2 |
| 24-25 | pad 3 |
| 26-27 | reserved |
| 28-31 | What type of header is this?  0 = Evio record header,  3 = Evio file trailer,  4 = HIPO record header,  7 = HIPO file trailer |

Bits 10-13 are only useful for the CODA online use of evio. That’s because, for CODA online, only a single CODA event ***type*** is placed into a single record, and each user or control event has its own record as well. Thus, all events will be of a single CODA type.

Following is a graphic displaying how the bit info word is laid out for both the file and record headers.

![](data:image/jpeg;base64...)

**TRAILER**

In an evio file, the ending record can, but does not have to be, a regular record. It can also take the form of a trailer. The trailer is a regular record header optionally followed by an uncompressed array of pairs of a record length (unit of bytes, 32-bit integer) and an event count – one pair for each record. This array is put in the place of the non-optional index array of events lengths that is part of the normal record header. See below.

![](data:image/png;base64...)

**TRAILER’S HEADER**

In either case, bit #9 of the bit info word in the record header needs to be set – indicating that this is the last record (bit #10 is the file header's "trailer has an index" flag). In the case of the trailer, bits 28-31 (seen in the table above) will indicate that this is a trailer. For an evio trailer, the value in those bits is 3. Also, the version number is placed in that same word, which makes the word 0x30000206 for a plain evio trailer. The event count is 0. Everything after the magic number is 0.

pyevio exposes the trailer as `EvioFile.trailer` and excludes it from `record_count` and `iter_records()`; `Record.scan_events()` returns no events for it, because its index array lists records.

MSB(31)                           LSB(0)
<-------------------- 32 bits ------------------>

|  |
| --- |
| **Record Length** |
| **Record Number** |
| **14** |
| **0** |
| **Index Array Length** |
| **0x 30 00 02 06** |
| **0** |
| **0xc0da0100** |
| **0** |
| **0** |
| **0** |
| **0** |

**FILE READING**

When reading a file, the reader will first read the file header. If available, it can get a list of record lengths from that header and from there calculate record positions.

If the file header’s index array is non-existent, the trailer position can be read. If it’s a valid value, the reader can jump to it, read the trailer, read the trailer’s index, and from there calculate the position of each record.

If no record length information is available in either the file header or the trailer, the reader will scan the whole file from beginning to end to obtain it.

pyevio always scans: it ignores the file header's index array and trailer position and steps from record header to record header by the record length word. Before a header is used it must hold the magic number at word 7, a header length of 8 or 14 with those words present, a record length of at least the header length, and no file type id in word 0 (which marks a second file header, as in two files concatenated). Bytes that fail this end the scan with a `("bad_record_header", offset, message)` entry in `EvioFile.checks`. A record whose length runs past the end of the file is kept as the last record, its complete events are read, and `("truncated_tail", declared_bytes, present_bytes, events_salvaged)` goes to `checks`. The scan continues past a record with the last-record bit set while another record header follows, because some writers set the bit on every record.

**SRO HARDWARE LAYOUT**

The raw files written by the JLab SRO hardware (for example `sro_000791.evio.00000`) carry EVIO v6 streaming events in a container that predates the v6 file layout:

* No file header. The first block header starts at byte 0.
* Every block header is 8 words: `{block length (words), block number, 8, event count, 0, 4, 0, 0xc0da0100}`. Word 5 is exactly 4: version 4 with no bit-info bits. Words 8—13 of the v6 record header (uncompressed length, compression, user registers) do not exist, so the blocks are uncompressed and hold no user header.
* No index array: the events follow the header directly and are walked by their length words.
* The last-record bit is never set and there is no trailer. The file ends after its last block, and the last block may be cut by the end of the file (3 of the 4 files of run 791 end this way).

The first block of an evio-library v4 file has the same shape, so a file without a file type id is identified by its whole header chain: every 8-word header must have word 5 equal to 4. A header with the last-block bit (0x200) or the dictionary bit (0x100) set, a version other than 4, or a 14-word header marks an evio-library file, and pyevio raises the version error for it. pyevio reports the layout as `EvioFile.header_layout == "sro-8word"`, with a stand-in `FileHeader` whose `version` is 6 (the content) and `container_version` 4 (the block headers); `trailer` is `None` and `record_count` comes from the scan, the cut tail included.

# EVIO Data Format

## Bank Structures & Content

EVIO data is composed of a hierarchy of banks of different types. Container banks contain other banks, and leaf banks contain an array of a single primitive data type. Three types of banks exist: BANK, SEGMENT, and TAGSEGMENT. BANK has a two-word header, the latter two have a one-word header. All banks contain a **length**, **tag** and **type**. BANK additionally has a **num** field. SEGMENT and TAGSEGMENT differ on the number of bits allocated to the tag and type. Tag and num are user-defined while type denotes the bank contents and the codes listed in the table below MUST be used or endian swapping will fail. Length is always the number of 32-bit longwords to follow (i.e. bank length minus one). New to this version of EVIO is the **pad** for both BANK and SEGMENT banks which indicates the number of bytes used for padding when type indicates 8 or 16 bit integers.

**BANK HEADER**

MSB 32 bits LSB

|  |
| --- |
| **length** |
| **tag | pad | type | num** |

Bits: 16 2 6 8

**SEGMENT HEADER**

|  |
| --- |
| **tag | pad | type | length** |

Bits: 8 2 6 16

**TAGSEGMENT HEADER**

|  |
| --- |
| **tag | type | length** |

Bits: 12 4 16

**CONTENT TYPES**

|  |  |
| --- | --- |
| **contentType** | **Primitive Data Type** |
| 0x0 | 32-bit unknown (not swapped) |
| 0x1 | 32 bit unsigned int |
| 0x2 | 32-bit float |
| 0x3 | 8-bit char\* |
| 0x4 | 16-bit signed short |
| 0x5 | 16-bit unsigned short |
| 0x6 | 8-bit signed char |
| 0x7 | 8-bit unsigned char |
| 0x8 | 64-bit double |
| 0x9 | 64-bit signed int |
| 0xa | 64-bit unsigned int |
| 0xb | 32-bit signed int |
| 0xc | TAGSEGMENT |
| 0xd | SEGMENT |
| 0xe | BANK |
| 0xf | Composite |
| 0x10 | BANK |
| 0x20 | SEGMENT |
| 0x21 | Hollerit\* |
| 0x22 | N value\* |
| 0x23 | n value\* |
| 0x24 | m value\* |

\*this type is only used internally for composite data

There are a few more things that the user must keep in mind:

* bank contents immediately follow the bank header
* the first bank in a buffer or event must be a BANK
* the CODA DAQ system defines specific conventions for tag and num values.

## Changes from Versions 1-3

There are a few changes from previous EVIO versions to take note of. A backwards-compatible change has been made for strings (type 0x3). Previously, a single ASCII, null-terminated string with undefined padding was contained in this type. Starting with version 4, an array of strings may be contained. Each string is separated by a null-termination (value of 0). A final termination of at least one 4 (ASCII char of value 4) is required in order to differentiate it from the earlier versions and to signify an end to the array. It is a self-padded type meaning it always ends on the 32 bit boundary.

Another change is that the type of 0x40, which was redundantly defined to be a TAGSEGMENT, has been removed since its value uses bits necessary to store the padding. This is unlikely to cause any problems since it was never used.

The pad in the BANK and SEGMENT types indicates the number of bytes used for padding to 32 bit boundaries when type indicates 8 or 16 bit integers (type = 0x4, 0x5, 0x6, or 0x7). For 16 bit types pad will be 0 or 2 while for the 8 bit types it will be 0-3. Unlike previous versions, this allows EVIO to contain odd numbers of these types with no ambiguity. For example, since a bank of 3 shorts is the same length as a bank of 4 shorts (banks must end on a 32 bit boundary) previously there was no way to tell if the last short was valid data or not. Now there is. Note, however, this is **not** the case with the TAGSEGMENT bank and so it is not recommended for storing these types.

## Composite Data Type

### General Type Info

A new type - Composite - has been added which originated with Hall B but also allows for future expansion if there is a need. Basically, the user specifies a custom format by means of a string. Although in practice it acts like a primitive type in that you can have a bank containing an array of them, a single Composite type looks more like 2 banks glued together. The first word comprises a TAGSEGMENT header which is followed by a string describing the data to come. After this TAGSEGMENT containing the data format string, is a BANK containing the actual data.

**COMPOSITE TYPE**

|  |
| --- |
| **tag | type | length** |
| data format string ... |
| **length** |
| **tag | pad | type | num** |
| actual data ... |

The routine to swap this data must be provided by the definer of the composite type - in this case Hall B. This swapping function is plugged into the EVIO library's swapping routine. Currently its types, tags, pad, and num values are not used. Only the lengths are significant.

There is actually another new type defined - the Hollerit type, but that is only used inside of the Composite type and refers to characters in an integer form. Following is a table of characters allowed in the data format string.

**DATA FORMAT CHARACTERS**

|  |  |
| --- | --- |
| **Data format char** | **Meaning** |
| ( | ( |
| ) | ) |
| i | 32-bit unsigned int |
| F | 32-bit floating point |
| a | 8-bit ASCII char |
| S | 16-bit short |
| s | 16-bit unsigned short |
| C | 8-bit char |
| c | 8-bit unsigned char |
| D | 64-bit double |
| L | 64-bit int |
| l | 64-bit unsigned int |
| I | 32-bit int |
| A | Hollerit |
| N | Multiplier as 32-bit int, |
| n | Multiplier as 16-bit int |
| m | Multiplier as 8-bit int |

In the format string, each of the allowed characters (except **N**, **n**, **m**) may be preceded by an integer which is a multiplier. Items are separated by commas. Instead of trying to explain the format abstractly, let's look at the following example:

i,L,2(s,2D,mF)

This format translates into the data being read and processed in the following order: a single 32-bit unsigned int, a single 64-bit int, a multiplier of 2 (32 bit int) of everything inside the parentheses which ends up being an unsigned short, 2 doubles, an 8-bit multiplier, multiplier number of 32-bit floats, an unsigned short, 2 doubles, an 8-bit multiplier, multiplier number of 32-bit floats. The data is read in according to this recipe.

There are a couple of data processing rules that are very important:

1. If the format ends but the end of the data is not reached, the format in the last parenthesis will be repeated until all data is processed. If there are no parentheses in the format, data processing will start again from the beginning of the format until all data is processed.
2. The explicitly given multiplier must be a number between 2 and 15 - inclusive. If the number of repeats is the symbol 'N' instead of a number, that multiplier will be read from data assuming 'I' format and may be any positive integer.

The Composite data type allows compact storage of different primitive data types and eliminates the need for extra banks and their accompanying headers. It does, however, pay a penalty in the amount of computing power needed to read, write, and swap it. For example, each time a Composite bank needs to be swapped, EVIO must read the format string, process it, and convert it into an array of ints. Then, with the converted format as a guide, EVIO must read through the data item-by-item, swapping each one. It is quite compute intensive.