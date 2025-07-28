# Jason Keller
# Feb 2021
# =============================================================================
#  Program to set BlackFly S camera settings and acquire frames from 2 synchronized cameras and 
#  write them to a compressed video file. Based on FLIR Spinnaker API example code. I have 
#  also tested with a Flea3 camera, which works but requires modifying the camera
#  settings section to use non "Quickspin" functions (see FLIR examples). 
# 
#  The intent is that this program started first, then will wait for triggers
#  on Line 0 (OPTO_IN) from the DAQ system. It is assumed that the DAQ system will provide
#  a specified number of triggers, and that the Line 0 black wires of both cameras are
#  soldered together and driven simultaneously. Both cameras output their "exposure active"
#  signal on Line 1 (OPTO_OUT, the white wire, which is pulled up to 3.3V via a 1.8kOhm resistor 
#  for each camera) so that each frame can be synchronized (DAQ should sample this at ~1kHz+).
#
#  Tkinter is used to provide a simple GUI to display the images, and skvideo 
#  is used as a wrapper to ffmpeg to write H.264 compressed video quickly, using
#  mostly default parameters (although I tried pix_fmt gray to reduce size further,
#  but default worked better)
#
#  To setup, you must download an FFMPEG executable and set an environment 
#  variable path to it (as well as setFFmpegPath function below). Other nonstandard
#  dependencies are the FLIR Spinnaker camera driver and PySpin package (see 
#  Spinnaker downloads), and the skvideo package. 
#  
#  NOTE: currently there is no check to see if readout can keep up with triggering
#  other that a timeout warning. It is up to the user to determine if the correct number
#  of frames are captured. Also, the "ffmpegThreads" parameter can throttle CPU usage
#  by FFMPEG to allow other data acquistion task priority. For example, with an Intel Xeon
#  W-2145 processor and 4 threads, CPU usage is limited to ~50-60% @ 500Hz, 320x240px,
#  and compressed writing is close to real-time.
#
# TO DO:
# (1) report potential # missed frames (maybe use counter to count Line 1 edges and write to video file)
# (2) try using ImageEvent instead of blocking GetNextImage(timeout) call
# (3) explicitly setup camera onboard buffer
# (4) use multiprocess or other package to implement better parallel processing
# (5) try FFMPEG GPU acceleration: https://developer.nvidia.com/ffmpeg
# =============================================================================

import PySpin, time, os, threading, queue, sys
from datetime import datetime
import tkinter as tk
from PIL import Image, ImageTk
import numpy as np
import skvideo
skvideo.setFFmpegPath(r'C:\Users\mouse1\AppData\Local\Programs\Python\Python38\Lib\site-packages\ffmpeg\6.0\bin') #set path to ffmpeg installation before importing io
import skvideo.io

#retrieve the arguments from the command
pulseDur = int(sys.argv[1])
mouseStr = sys.argv[2]
blockNb =  sys.argv[3]

#constants
SAVE_FOLDER_ROOT = 'C:/video'
FILENAME_ROOT = 'behvid_' # optional identifier
EXPOSURE_TIME = 500 # in microseconds
GAIN_VALUE = 0 #in dB, 0-40;
GAMMA_VALUE = 0.5 #0.25-1
SEC_TO_RECORD = pulseDur #approximate # seconds to record for; can also use Ctrl-C to interupt in middle of capture
IMAGE_HEIGHT = 440  #540 pixels default 
IMAGE_WIDTH = 360 #720 pixels default WIDTH:HEIGHT ratio = 4:3
HEIGHT_OFFSET = round((540-IMAGE_HEIGHT)/2) # Y, to keep in middle of sensor
WIDTH_OFFSET = round((720-IMAGE_WIDTH)/2) # X, to keep in middle of sensor
WAIT_TIME = 0.0001 #in seconds - this limits polling time and should be less than the frame rate period 
CAM_TIMEOUT = 1000 #in ms; time to wait for another image before aborting
#FRAME_RATE_OUT = 250

# generate output video directory and filename and make sure not overwriting
now = datetime.now()
#mouseStr = input("Enter mouse ID: ") #if running from python
dateStr = now.strftime("%Y_%m_%d") #save folder ex: 2020_01_01
timeStr = now.strftime("%H_%M_%S") 
saveFolder = SAVE_FOLDER_ROOT + '/' + dateStr
if not os.path.exists(saveFolder):
    os.mkdir(saveFolder)
os.chdir(saveFolder)
movieName = FILENAME_ROOT + timeStr + '_' + mouseStr + '_' + blockNb + '.mp4'
fullFilePath = [saveFolder + '/' + movieName]
print('Video will be saved to: {}'.format(fullFilePath))

# get frame rate and query for video length based on this
frameRate = 200 # MUST MATCH WITH EXTERNAL PULSES # FOR FREE RUN: cam1.AcquisitionResultingFrameRate()
print('frame rate = {:.2f} FPS'.format(frameRate))
numImages = round(frameRate*SEC_TO_RECORD)
print('# frames = {:d}'.format(numImages))

# SETUP FUNCTIONS #############################################################################################################
def initCam(cam): #function to initialize camera parameters for synchronized capture
    cam.Init()
    # load default configuration
    cam.UserSetSelector.SetValue(PySpin.UserSetSelector_Default)
    cam.UserSetLoad()
    # set acquisition. Continues acquisition. Auto exposure off. Set frame rate using exposure time. 
    cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)
    cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
    cam.ExposureMode.SetValue(PySpin.ExposureMode_Timed) #Timed or TriggerWidth (must comment out trigger parameters other that Line)
    cam.ExposureTime.SetValue(EXPOSURE_TIME)
    cam.AcquisitionFrameRateEnable.SetValue(False)
    # set analog. Set Gain + Gamma. 
    cam.GainAuto.SetValue(PySpin.GainAuto_Off)
    cam.Gain.SetValue(GAIN_VALUE)
    cam.GammaEnable.SetValue(True)
    cam.Gamma.SetValue(GAMMA_VALUE)
    # set ADC bit depth and image pixel depth, size
    cam.AdcBitDepth.SetValue(PySpin.AdcBitDepth_Bit10)
    cam.PixelFormat.SetValue(PySpin.PixelFormat_Mono8)
    cam.Width.SetValue(IMAGE_WIDTH)
    cam.Height.SetValue(IMAGE_HEIGHT)
    cam.OffsetX.SetValue(WIDTH_OFFSET)
    cam.OffsetY.SetValue(HEIGHT_OFFSET)
    # setup FIFO buffer
    camTransferLayerStream = cam.GetTLStreamNodeMap()
    handling_mode1 = PySpin.CEnumerationPtr(camTransferLayerStream.GetNode('StreamBufferHandlingMode'))
    handling_mode_entry = handling_mode1.GetEntryByName('OldestFirst')
    handling_mode1.SetIntValue(handling_mode_entry.GetValue())
    # set trigger input to Line0 (the black wire)
    cam.TriggerMode.SetValue(PySpin.TriggerMode_On)
    cam.TriggerOverlap.SetValue(PySpin.TriggerOverlap_ReadOut) #Off or ReadOut to speed up
    cam.TriggerSource.SetValue(PySpin.TriggerSource_Line0)
    cam.TriggerActivation.SetValue(PySpin.TriggerActivation_RisingEdge) #LevelHigh or RisingEdge
    cam.TriggerSelector.SetValue(PySpin.TriggerSelector_FrameStart) # require trigger for each frame
    # optionally send exposure active signal on Line 2 (the white wire)
    cam.LineSelector.SetValue(PySpin.LineSelector_Line1)
    cam.LineMode.SetValue(PySpin.LineMode_Output) 
    cam.LineSource.SetValue(PySpin.LineSource_ExposureActive) #route desired output to Line 1 (try Counter0Active or ExposureActive)
    #cam.LineSelector.SetValue(PySpin.LineSelector_Line2)
    #cam.V3_3Enable.SetValue(True) #enable 3.3V rail on Line 2 (red wire) to act as a pull up for ExposureActive - this does not seem to be necessary as long as a pull up resistor is installed between the physical lines, and actually degrades signal quality 
    
def saveImage(imageWriteQueue, writer): #function to save video frames from the queue in a separate process
    while True:
        dequeuedImage = imageWriteQueue.get()
        if dequeuedImage is None:
            break
        else:
            writer.writeFrame(dequeuedImage) #call to ffmpeg
            imageWriteQueue.task_done()
                      
def camCapture(camQueue, cam, k): #function to capture images, convert to numpy, send to queue, and release from buffer in separate process
    while True:
        if k == 0: #wait infinitely for trigger for first image
            image = cam.GetNextImage() #get pointer to next image in camera buffer; blocks until image arrives via USB, within infinite timeout for first frame while waiting for DAQ to start sending triggers    
        elif (k) == (numImages):
            print('cam done ')
            break #stop loop and function when expected # frames found
        else:
            try:
                image = cam.GetNextImage(CAM_TIMEOUT) #get pointer to next image in camera buffer; blocks until image arrives via USB, within CAM_TIMEOUT
            except: #PySpin will throw an exception upon timeout, so end gracefully
                print('WARNING: timeout waiting for trigger! Aborting...press Ctrl-C to stop')
                print(str(k) + ' frames captured')
                break
                    
        npImage = np.array(image.GetData(), dtype="uint8").reshape( (image.GetHeight(), image.GetWidth()) ); #convert PySpin ImagePtr into numpy array; use uint8 for Mono8 images, uint16 for Mono16
        camQueue.put(npImage)  
        image.Release() #release from camera buffer
        k = k + 1

# INITIALIZE CAMERAS & COMPRESSION ###########################################################################################
system = PySpin.System.GetInstance() # Get camera system
cam_list = system.GetCameras() # Get camera list
cam1 = cam_list[0]
cam2 = cam_list[1]
initCam(cam1) 
initCam(cam2) 
 
# setup output video file parameters (can try H265 in future for better compression):  
# for some reason FFMPEG takes exponentially longer to write at nonstandard frame rates, so just use default 25fps and change elsewhere if needed
crfOut = 21 #controls tradeoff between quality and storage, see https://trac.ffmpeg.org/wiki/Encode/H.264 
ffmpegThreads = 4 #this controls tradeoff between CPU usage and memory usage; video writes can take a long time if this value is low
#crfOut = 18 #this should look nearly lossless
#writer = skvideo.io.FFmpegWriter(movieName, outputdict={'-r': str(FRAME_RATE_OUT), '-vcodec': 'libx264', '-crf': str(crfOut)}) # with frame rate
writer = skvideo.io.FFmpegWriter(movieName, outputdict={'-vcodec': 'libx264', '-crf': str(crfOut), '-threads': str(ffmpegThreads)})

#setup tkinter GUI (non-blocking, i.e. without mainloop) to output images to screen quickly
window = tk.Tk()
window.title("camera acquisition")
geomStrWidth = str(IMAGE_WIDTH*2 + 25)
geomStrHeight = str(IMAGE_HEIGHT + 35)
window.geometry(geomStrWidth + 'x' + geomStrHeight) # 2x width+25 x height+35; large enough for frames from 2 cameras + text
#textlbl = tk.Label(window, text="elapsed time: ")
textlbl = tk.Label(window, text="waiting for trigger...")
textlbl.grid(column=0, row=0)
imglabel = tk.Label(window) # make Label widget to hold image
imglabel.place(x=10, y=20) #pixels from top-left
window.update() #update TCL tasks to make window appear

#############################################################################
# start main program loop ###################################################
#############################################################################    

try:
    print('Press Ctrl-C to exit early and save video')
    i = 0
    imageWriteQueue = queue.Queue() #queue to pass images captures to separate compress and save thread
    cam1Queue = queue.Queue()  #queue to pass images from separate cam1 acquisition thread
    cam2Queue = queue.Queue()  #queue to pass images from separate cam2 acquisition thread
    # setup separate threads to accelerate image acquisition and saving, and start immediately:
    saveThread = threading.Thread(target=saveImage, args=(imageWriteQueue, writer,))
    cam1Thread = threading.Thread(target=camCapture, args=(cam1Queue, cam1, i,))
    cam2Thread = threading.Thread(target=camCapture, args=(cam2Queue, cam2, i,))
    saveThread.start()  
    
    cam1.BeginAcquisition()
    cam2.BeginAcquisition()
    cam1Thread.start()
    cam2Thread.start()   

    for i in range(numImages): # main acquisition loop
        camsNotReady = (cam1Queue.empty() or cam2Queue.empty()) # wait for both images ready from parallel threads
        while camsNotReady: #wait until ready in a loop
            time.sleep(WAIT_TIME)
            camsNotReady = (cam1Queue.empty() or cam2Queue.empty()) # wait for both images ready
           
        if i == 0:
            tStart = time.time()
            print('Capture begins')
        dequeuedAcq1 = cam1Queue.get() # get images formated as numpy from separate process queues as soon as they are both ready
        dequeuedAcq2 = cam2Queue.get()
        
        # now send concatenated image to FFMPEG saving queue
        enqueuedImageCombined = np.concatenate((dequeuedAcq1, dequeuedAcq2), axis=1)
        imageWriteQueue.put(enqueuedImageCombined) #put next combined image in saving queue
        
        if (i+1)%20 == 0: #update screen every X frames 
#            timeElapsed = str(time.time() - tStart)
#            timeElapsedStr = "elapsed time: " + timeElapsed[0:5] + " sec"
            framesElapsedStr = "frame #: " + str(i+1)
            textlbl.configure(text=framesElapsedStr)
            I = ImageTk.PhotoImage(Image.fromarray(enqueuedImageCombined))
            imglabel.configure(image=I)
            imglabel.image = I #keep reference to image
            window.update() #update on screen (this must be called from main thread)

        if (i+1) == (numImages):
            print('Complete ' + str(i+1) + ' frames captured')
            tEndAcq = time.time()

# end aqcuisition loop #############################################################################################            
except KeyboardInterrupt: #if user hits Ctrl-C, everything should end gracefully
    tEndAcq = time.time()
    pass        
        
cam1.EndAcquisition() 
cam2.EndAcquisition()
textlbl.configure(text='Capture complete, still writing to disk...') 
window.update()
print('Capture ends at: {:.2f}sec'.format(tEndAcq - tStart))
#   print('calculated frame rate: {:.2f}FPS'.format(numImages/(t2 - t1)))
imageWriteQueue.join() #wait until compression and saving queue is done writing to disk
tEndWrite = time.time()
print('File written at: {:.2f}sec'.format(tEndWrite - tStart))
writer.close() #close to FFMPEG writer
window.destroy() 
    
# delete all pointers/variable/etc:
cam1.DeInit()
cam2.DeInit()
del cam1
del cam2
cam_list.Clear()
del cam_list
system.ReleaseInstance()
del system
print('Done!')

os._exit(0) # This is critical to keep the command prompt alive after each run. 