#!/usr/bin/env python

import sys
import argparse
import csv
import errno
import curses
import os


stdin_fd_copy = None
READ_CHUNK_SIZE = 2000


def debug(s):
    with open('./xcol_debug.info', 'w') as f:
        f.write(s)

class Display:
    def __init__(self):
        self.eof = False
        self.allLines = []
        self.maxColW = []  # The width of each column
        self.widthLimit = None
        self.params = None
        self.reader = None
        self.fh = None
        self.linesRead = 0
        self.comment = None
        self.sep = None
        self.readChunkSize = READ_CHUNK_SIZE
        # Display
        self.scr = None
        self.SCR_SIZE = None
        self.ACTIVE_COLUMN = -1  # index of the column that's chosen. -1 if no chosen.
        self.COLUMN_MASK = []  # if column is to be hidden [1,1,0,1]
        self.X = 0  # column index, actual x coordinate
        self.Y = 0  # row index, as in the file, start drawing from this row.
        self.Y_sub = 0  # at which sub-row in a wrapped row, an add-and-carry relationship with self.Y_lines
        self.Y_lines = 0  # lines needed for a single row
        self.Y_OFFSET = 1  # for sys row of column selection, by character
        self.Y_LOW_OFFSET = 1
        self.X_OFFSET = 0  # for sys col of row selection, by character
        self.Y_FREEZE_ST = -1  # freeze from this row
        self.X_FREEZE_ST = -1
        self.Y_FREEZE_ED = -1  # freeze at a certain data row
        self.X_FREEZE_ED = -1
        self.PAD_BETWEEN_COL = 4

    def readParams(self):
        global stdin_fd_copy
        parser = argparse.ArgumentParser()
        parser.add_argument('-t', dest='file_type', default='tsv', choices=['tsv', 'csv', 'vcf', 'ssv', 'ws'])
        parser.add_argument('-C', dest='consecutive_delimiter', action='store_true', default=False)
        parser.add_argument('-w', dest='max_col_w', type=int, default=100000000)
        parser.add_argument('-c', dest='comment', nargs='+', default=[])
        parser.add_argument('-H', dest='hide_overflow', action='store_true', default=False)
        # parser.add_argument('-hc', dest='hide_comment', action='store_true', default=False)
        if not stdin_fd_copy:
            parser.add_argument('input_file', default=None)  # if input by stdin, not from file.

        p = parser.parse_args()
        self.params = p
        self.widthLimit = p.max_col_w

        defaults = {'tsv': ('\t', [],    ),
                    'csv': (',',  [],    ),
                    'vcf': ('\t', ['##'],),
                    'ssv': (' ',  [],    ),
                    'ws':  ('\t', [],    ),  # white space
                    }
        self.sep = defaults[p.file_type][0]
        self.comment = defaults[p.file_type][1] + p.comment
        self.fh = open(p.input_file, 'rb') if not stdin_fd_copy else os.fdopen(stdin_fd_copy)
        self.reader = csv.reader(self.fh, delimiter=self.sep)

    def isValidRow(self, cells):
        if not cells:
            return False
        for c in self.comment:
            if cells[0].startswith(c):
                return False
        return True

    def read(self, step):
        if not self.eof:
            try:
                cnt = step
                while cnt:  # break when cnt == 0
                    cnt -= 1
                    cells = self.reader.next()  # it does not convert data type, i guess..
                    if self.isValidRow(cells):
                        # Keep splitting
                        if self.params.consecutive_delimiter:
                            cells = [c for c in cells if c]
                        if self.params.file_type == 'ws':
                            _nested = [c.split(' ') for c in cells if c]
                            cells = [c for sub in _nested for c in sub if c]  # squash nested list
                        # Determine column width
                        for j, w in enumerate(map(len, map(str, cells))):
                            if j >= len(self.maxColW):
                                self.maxColW.append(w)
                                self.COLUMN_MASK.append(1)
                            # j is also displayed in the header
                            self.maxColW[j] = min(max(self.maxColW[j], w, j), self.widthLimit)
                    self.linesRead += 1
                    self.allLines.append(tuple(cells))
            except StopIteration:
                self.eof = True

    def getFmtStr(self, width, is_comment=False, at_line=-1):
        sep = self.sep if is_comment else ' ' * self.PAD_BETWEEN_COL
        raw_fmt_str = sep.join(['{:<%d}' % w for w in self.maxColW[:width]])
        # if at_line >= 0:
        #     raw_fmt_str = '{:<12}'.format(at_line) + raw_fmt_str
        return raw_fmt_str

    def padStr(self, s):
        return s + ' ' * (self.SCR_SIZE[1] - len(s) - self.X_OFFSET)

    def display(self):
        active_win_h = self.SCR_SIZE[0] - self.Y_OFFSET - self.Y_LOW_OFFSET
        active_win_x = self.SCR_SIZE[1] - self.X_OFFSET
        # Draw header
        # total_len = 0
        # for i, head_len, head_str in enumerate(self.getHeader()):
        #     br = False
        #     total_len += head_len
        #     if total_len >= active_win_x:
        #         head_str = head_str[:total_len-active_win_x]
        #         br = True
        #     self.draw_raw(0, self.X_OFFSET + total_len, head_str, curses.color_pair(10))
        #     if br:
        #         break
        # TODO: I was working on header display, below line works, upper part do not.
        self.draw_raw(0, self.X_OFFSET,
                      self.padStr(''.join([_ for i, _ in self.getHeader()])[self.X: self.X + active_win_x]),
                      curses.color_pair(10)|curses.A_BOLD)
        to_draw = []
        # added self.Y_sub, prepare a bit more and draw from Y_sub.
        for cnt, h in enumerate(range(self.Y, self.Y + active_win_h + self.Y_sub)):
            try:
                cells = self.allLines[h]
            except IndexError:
                # reached eof, or not loaded, maybe.
                to_draw.append(' '*self.SCR_SIZE[1])
                continue
            fmt_str = self.getFmtStr(len(cells), at_line=h)
            # For Comments
            if not self.isValidRow(cells):
                to_draw.append(self.padStr(fmt_str.format(*cells)[self.X: self.X+active_win_x]))
                continue
            # For Contents
            line_needed = self.linesNeeded(cells)
            for i in range(line_needed):
                str_ele = [cell[i*self.maxColW[idx]:(i+1)*self.maxColW[idx]] for idx, cell in enumerate(cells)]
                to_draw.append(self.padStr(fmt_str.format(*str_ele)[self.X: self.X+active_win_x]))
            h += line_needed - 1
        [self.draw(cnt, 0, s) for cnt, s in enumerate(to_draw[self.Y_sub:self.Y_sub + active_win_h])]

    def linesNeeded(self, cells):
        if not self.params.hide_overflow and self.isValidRow(cells):
            return max([-(-len(c) // self.maxColW[i]) for i, c in enumerate(cells)])
        else:
            return 1

    def closeFile(self):
        if self.fh: self.fh.close()

    def changeWidth(self, offset):
        if self.ACTIVE_COLUMN != -1:
            if offset < 0 and self.maxColW[self.ACTIVE_COLUMN] <= 5:
                return
            self.maxColW[self.ACTIVE_COLUMN] += offset

    def onMouseClick(self, y, x):
        # first row is always fixed for choosing columns. X Y both starts from 0
        # TODO
        pass

    def getHeader(self):
            # str_ele = []
            for i, col_w in enumerate(self.maxColW):
                # str_ele.append('{:^{}}'.format(i + 1, col_w + self.PAD_BETWEEN_COL))
                yield col_w + self.PAD_BETWEEN_COL, '{:^{}}|'.format(i + 1, col_w + self.PAD_BETWEEN_COL - 1)
            # return ''.join(str_ele)

    def moveH(self, offset):
        if offset < 0 and self.X + offset <= self.X_FREEZE_ED + 1:
            self.X = self.X_FREEZE_ED + 1
            return
        self.X += offset

    def moveV(self, offset, to_end=False, to_head=False):
        if to_head:
            self.Y = self.Y_FREEZE_ED + 1
            return

        direction = offset / abs(offset)

        if len(self.allLines) == 0:
            return
        if direction < 0 and self.Y_sub == 0 and self.Y > 0:
            self.Y_lines = self.linesNeeded(self.allLines[self.Y-1])
        else:
            self.Y_lines = self.linesNeeded(self.allLines[self.Y])

        if not self.eof and direction > 0 and self.linesRead - self.Y < max(self.readChunkSize, offset):
            self.read(self.readChunkSize)
        cnt = abs(offset) if not to_end else -1
        while cnt:
            cnt -= 1
            old_y_sub = self.Y_sub
            self.Y_sub += direction
            if (direction > 0 and self.Y_sub >= self.Y_lines) or (direction < 0 and self.Y_sub < 0):
                self.Y_sub = 0 if direction > 0 or self.Y ==0 else self.Y_lines - 1
                # In case of underflow (underflow raises no exception)
                if direction < 0 and self.Y + direction <= self.Y_FREEZE_ED + 1:
                    self.Y = self.Y_FREEZE_ED + 1
                    break
                try:
                    self.Y_lines = self.linesNeeded(self.allLines[self.Y + direction])
                    self.Y += direction
                except IndexError:
                    self.Y_sub = old_y_sub
                    break  # In case of overflow


    #############
    #  DISPLAY  #
    #############
    def _colorAssignment(self):
        curses.start_color()  # initialize right after screen is created
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_MAGENTA, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(5, curses.COLOR_BLUE, curses.COLOR_BLACK)
        curses.init_pair(6, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(9, curses.COLOR_WHITE, curses.COLOR_RED)
        curses.init_pair(10, curses.COLOR_WHITE, curses.COLOR_GREEN)
        curses.init_pair(11, curses.COLOR_YELLOW, curses.COLOR_RED)
        curses.init_pair(12, curses.COLOR_YELLOW, curses.COLOR_GREEN)
        # curses.init_pair(80, curses.COLOR_WHITE, curses.COLOR_YELLOW)

    def _setup(self):
        self.scr = curses.initscr()
        self._colorAssignment()
        curses.noecho()
        curses.cbreak()
        curses.mousemask(1)
        self.scr.keypad(True)

    def _terminate(self):
        curses.nocbreak()
        self.scr.keypad(0)
        curses.echo()
        curses.endwin()
        # sys.exit()

    def draw(self, y, x, s):
        # y and x are coordinate in the content window, not the full window.
        # Coordinates starts from 0.
        self.scr.addstr(y + self.Y_OFFSET, x + self.X_OFFSET, s)

    def draw_raw(self, y, x, s, color):
        self.scr.addstr(y, x, s, color)

    def show(self):
        self._setup()
        while 1:
            self.SCR_SIZE = self.scr.getmaxyx()
            self.display()
            # self.draw(1,1,'{} {} '.format(self.Y, self.Y_sub))
            event = self.scr.getch()
            if event == ord('q'):
                self._terminate()
                break
            elif event == ord(','): # '<'
                self.changeWidth(-1)
            elif event == ord('.'): # '>'
                self.changeWidth(1)
            elif event == curses.KEY_LEFT:
                self.moveH(-20)
            elif event == curses.KEY_RIGHT:
                self.moveH(+20)
            elif event == curses.KEY_UP:
                self.moveV(-1)
            elif event == curses.KEY_DOWN:
                self.moveV(+1)
            elif event == curses.KEY_PPAGE:
                y_occupied = self.Y_FREEZE_ED - self.Y_FREEZE_ST + self.Y_OFFSET + self.Y_LOW_OFFSET
                self.moveV(-(self.SCR_SIZE[0] - y_occupied))
            elif event == curses.KEY_NPAGE:
                y_occupied = self.Y_FREEZE_ED - self.Y_FREEZE_ST + self.Y_OFFSET + self.Y_LOW_OFFSET
                self.moveV(self.SCR_SIZE[0] - y_occupied)
            elif event == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, _ = curses.getmouse()
                    self.onMouseClick(my, mx)
                except:
                    pass # there're some errors with the mouse scroll
            elif event == ord('G'):
                self.read(-1)
                self.moveV(1, to_end=True)
            elif event == ord('g'):
                self.moveV(-1, to_head=True)


def main():
    global stdin_fd_copy
    if not sys.stdin.isatty():  # not attached to a terminal
        stdin_fd_copy = os.dup(sys.stdin.fileno())
        os.close(0)
        sys.stdin = open('/dev/tty')

    d = Display()
    d.readParams()
    try:
        d.read(READ_CHUNK_SIZE)
        d.show()
    except IOError as e:  # broken pipe exception
        if e.errno == errno.EPIPE:
            pass
    except KeyboardInterrupt:
        d._terminate()
        sys.exit('Keyboard Interrupted')
    except:
        d._terminate()
        import traceback
        sys.exit(traceback.format_exc())
    finally:
        if d:
            d.closeFile()

main()
