import socket
import os


def read_exact_bytes(sock,n):
    """read exactly n bytes from socket,
    data is an empty byte string,
      keeps reading until read full n bytes data """
    data = b""   #empty byte string
    while len(data) < n:
        chunk = sock.recv(n-len(data))
        if not chunk:
            raise ConnectionError("Connection closed early")
        data += chunk
    return data


def send_8units(sock,value):
    """ sends a single unsigned 1 byte( 8 bits)  integer, 
    takes socket and numeric label (value) )"""
    #makes sure its 1 byte only
    if not (0 <= value <= 255):
        raise ValueError("send_u8: value must be between 0 and 255")
    sock.sendall(value.to_bytes(1, "big"))  #converts integer to 1 byte, "big" refers to it being big-endian

def recv_8units(sock):
    """Recives a 1 byte integer from the socket and return it as an int"""
    return int.from_bytes(read_exact_bytes(sock,1),"big")

def send_16units(sock, value):
    """sends an unsigned 2 bytes (16 bit)  integer through the socket"""
    if not ( 0 <= value <= 65535):
        raise ValueError("send_u16: value must be between 0 and 65,535")
    sock.sendall(value.to_bytes(2, "big"))

def recv_16units(sock):
    """Recive an unsigned 2 bytes integer from the socket and return it as an int"""
    return int.from_bytes(read_exact_bytes(sock,2),"big")

def send_64units(sock,value):
    """send an unsigned 8 bytes (64 bits) integer through the socket """
    if not (0 <= value <= 18446744073709551615):
        raise ValueError ("send_u64 : value must be between 0 and 18,446,744,073,709,551,615")
    sock.sendall(value.to_bytes(8,"big"))

def recv_64units(sock):
    """Recives an 8 byte unsigned integer from the socket and return it"""
    return int.from_bytes(read_exact_bytes(sock,8),"big")

def check_filename(name):
    """return true if filename is valid and safe"""
    #if no filename or if it is longer than 255 (most OS cant handle over 255 characters filename) return false
    if not name or len(name) > 255:
        return False
    #making sure no directory is allowed
    if "/" in name or "\\" in name or ".." in name:
        return False
    #convert filename characters into ASCII code
    #loops through each character in filename and checks its ASCII code
    return all(32 <= ord(ch) < 127 for ch in name)

def is_image(name):
    """checks if a filename ends with jpg / jpeg / png """
    n = name.lower()
    return n.endswith(".jpg") or n.endswith(".jpeg") or n.endswith(".png")

def is_jpeg_header(header):
    return len(header) >=2 and header[:2]==b"\xFF\xD8"

#same idea but with png 
def is_png_header(header):
    return len(header) >=8 and header[:8]== b"\x89PNG\r\n\x1a\n"
