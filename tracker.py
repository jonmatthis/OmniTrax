import numpy as np

try:
    from omni_trax.kalman_filter_new import KalmanFilter
except ModuleNotFoundError:
    from kalman_filter_new import KalmanFilter
from scipy.optimize import linear_sum_assignment


class Track(object):
    """
    Individual Track class (instances created from Tracker)
    Including Kalman-Filter based buffer-and-recover tracking
    """

    def __init__(self, prediction, trackIdCount,
                 dt=0.033, u_x=0, u_y=0,
                 std_acc=5, y_std_meas=0.1, x_std_meas=0.1,
                 predicted_class=None,
                 bbox=[None, None, None, None],
                 known_id=-1):
        """
        Initialise individual track
        :param prediction: [x,y] coordinates of input (detection)
        :param trackIdCount: current track number to assign unique names
        :param dt: time interval between two consecutive updates
        :param u_x: acceleration in x-direction
        :param u_y: acceleration in y-direction
        :param std_acc: process noise magnitude
        :param x_std_meas: standard deviation of the measurement in x-direction
        :param y_std_meas: standard deviation of the measurement in y-direction
        :param prediction: initial location [x,y] of track
        :param bbox: bounding box of detection
        :param known_id: assigned ID, when initialising from a prior tracker state
        """
        if known_id != -1:
            self.track_id = known_id  # use previous ID, when initialising from prior tracked state
        else:
            self.track_id = trackIdCount  # identification of each track object
        self.KF = KalmanFilter(dt=dt, u_x=u_x, u_y=u_y,
                               std_acc=std_acc, y_std_meas=y_std_meas, x_std_meas=x_std_meas,
                               initial_state=prediction)  # KF instance to track this object
        self.prediction = np.asarray(prediction)  # predicted centroids (x,y)
        self.skipped_frames = 0  # number of frames skipped undetected
        self.trace = []  # trace path
        if any(bbox) is not None:
            self.bbox_trace = [bbox]  # trace bounding boxes
        if predicted_class is not None:
            # we create a list of predicted classes for each frame, so when terminating the track,
            # we can perform a majority vote to determine the most likely class.
            # Additionally, at sufficient class resolution, the predicted class can be used as part of an extended cost
            # function when linking detections to existing tracks.
            self.predicted_class = [predicted_class]


class Tracker(object):
    """
    Multi object tracking class implementing the Hungarian Matching algorithm and adding customisable
    Kalman-Filter based buffer-and-recover tracking
    """

    def __init__(self, dist_thresh, max_frames_to_skip, max_trace_length,
                 trackIdCount, use_kf=False, dt=0.033, u_x=0, u_y=0,
                 std_acc=5, y_std_meas=0.1, x_std_meas=0.1):
        """
        Initialise Tracker Class
        :param dist_thresh: maximum squared distance between a track and a detection to be considered for matching
        :param max_frames_to_skip: number of frames to buffer (attempt to reassign a track) before termination
        :param max_trace_length: number of detections stored in track (relevant for display only, as valid tracks are
                                 written to tracked clip as markers
        :param trackIdCount: begin counting tracks from initial value (0 by default)
        :param use_kf: boolean, use Kalman Filter tracking
        :param dt: sampling time (time for 1 cycle)
        :param u_x: acceleration in x-direction
        :param u_y: acceleration in y-direction
        :param std_acc: process noise magnitude
        :param x_std_meas: standard deviation of the measurement in x-direction
        :param y_std_meas: standard deviation of the measurement in y-direction
        """
        self.dist_thresh = dist_thresh
        self.max_frames_to_skip = max_frames_to_skip
        self.max_trace_length = max_trace_length
        self.tracks = []
        self.trackIdCount = trackIdCount
        self.use_kf = use_kf
        self.dt = dt
        self.u_x = u_x
        self.u_y = u_y
        self.std_acc = std_acc
        self.y_std_meas = y_std_meas
        self.x_std_meas = x_std_meas

    def initialise_from_prior_state(self, prior_state):
        """
        Initialise Tracker from prior tracked state
        :param prior_state: [id,x,y] of prior track
        """
        # create new track from the last increment of the prior state, keeping ID's intact
        track = Track([[prior_state[1]], [prior_state[2]]],
                      self.trackIdCount, known_id=prior_state[0],
                      dt=self.dt, u_x=self.u_x, u_y=self.u_y, std_acc=self.std_acc,
                      y_std_meas=self.y_std_meas, x_std_meas=self.x_std_meas,
                      predicted_class=prior_state[3],
                      bbox=prior_state[4])
        self.trackIdCount += 1
        self.tracks.append(track)

    def set_trackIdCount(self, latest_trackid):
        """
        Overwrite trackIDCount to start counting tracks from value other than 0
        (Useful, when starting prior tracked state to not overwrite previous tracks that share the same name)
        :param latest_trackid: integer, desired starting value for counting track IDs
        """
        self.trackIdCount = int(latest_trackid) + 1

    def clear_tracks(self):
        """
        Clear all existing tracks, while leaving the Tracker settings intact
        """
        self.tracks = []

    def Update(self, detections, predicted_classes=None, bounding_boxes=None):
        """
        Update tracks vector using following steps:
            - Create tracks from detections if no tracks exist
            - Calculate assignment cost (squared distance between tracks vs detections)
            - Using Hungarian Algorithm match detections and tracks based on their assignment cost
            - Identify tracks with no assignment, if any
                - When tracks are not reassigned to a detection for > max_frames_to_skip, remove them
                - Start new tracks from unassigned detections
            - Update KalmanFilter state, lastResults and tracks trace
        :param detections: list of darknet detection centres from current frame
        :param predicted_classes: predicted classes from darknet detections
                                  (in the same order as theirs respective detections)
        :param bounding_boxes: predicted bounding from darknet detections
                               (in the same order as theirs respective detections)
        """

        # Create tracks if no tracks vector was found
        if len(self.tracks) == 0:
            for i in range(len(detections)):
                track = Track(detections[i], self.trackIdCount,
                              dt=self.dt, u_x=self.u_x, u_y=self.u_y, std_acc=self.std_acc,
                              y_std_meas=self.y_std_meas, x_std_meas=self.x_std_meas,
                              predicted_class=predicted_classes[i],
                              bbox=bounding_boxes[i])
                self.trackIdCount += 1
                self.tracks.append(track)

        # Calculate cost using euclidean distance between
        # predicted vs detected centroids
        N = len(self.tracks)
        M = len(detections)
        cost = np.zeros(shape=(N, M))  # Cost matrix

        for i in range(N):
            for j in range(len(detections)):
                diff = self.tracks[i].prediction[:2] - detections[j]
                distance = np.sqrt(diff[0][0] * diff[0][0] +
                                   diff[1][0] * diff[1][0])
                cost[i][j] = distance

        # add columns equal to the number of tracks, so that if a track cannot be assigned to
        # a detection, it is instead assigned to a placeholder instead to avoid forced incorrect matches.
        # This step also removes the need to filter for "unmatchable" tracks due to large distances
        cost = np.c_[cost, np.ones((N, N)) * self.dist_thresh]

        # Use hungarian algorithm to find likely matches, minimising cost
        assignment = []
        for _ in range(N):
            assignment.append(-1)

        row_ind, col_ind = linear_sum_assignment(cost)

        for i in range(len(col_ind)):
            # lowest cost along the diagonal
            assignment[i] = col_ind[i]

        # Identify tracks with no assignment, if any
        un_assigned_tracks = []
        for i in range(N):
            if assignment[i] == -1 or assignment[i] >= M:
                # check for cost distance threshold.
                # If cost is very high then un_assign (delete) the track
                print("cost to assign", i, "is =", cost[i][assignment[i]])
                assignment[i] = -1
                un_assigned_tracks.append(i)
                self.tracks[i].skipped_frames += 1
                print("Track", i, "has been invisible for", self.tracks[i].skipped_frames, "frames!")

        print("Unassigned tracks:", un_assigned_tracks, "\n")

        # If tracks are not detected for a long time, remove them
        del_tracks = []
        for i in range(N):
            if self.tracks[i].skipped_frames > self.max_frames_to_skip:
                del_tracks.append(i)

        if len(del_tracks) > 0:  # only when skipped frames exceeds max
            for id in del_tracks:
                if id < len(self.tracks):
                    print("\n!!!! Deleted track:", self.tracks[id].track_id, "\n !!!!")
                    del self.tracks[id]
                    del assignment[id]
                else:
                    print("something unexpected assignment error...")

        # Now look for un_assigned detects
        un_assigned_detects = []
        for i in range(M):
            if i not in assignment:
                un_assigned_detects.append(i)

        # Start new tracks
        if len(un_assigned_detects) != 0:
            for i in range(len(un_assigned_detects)):
                if predicted_classes is not None and any(bounding_boxes) is not None:
                    track = Track(detections[un_assigned_detects[i]],
                                  self.trackIdCount,
                                  dt=self.dt, u_x=self.u_x, u_y=self.u_y, std_acc=self.std_acc,
                                  y_std_meas=self.y_std_meas, x_std_meas=self.x_std_meas,
                                  predicted_class=predicted_classes[un_assigned_detects[i]],
                                  bbox=bounding_boxes[un_assigned_detects[i]])
                else:
                    track = Track(detections[un_assigned_detects[i]],
                                  self.trackIdCount,
                                  dt=self.dt, u_x=self.u_x, u_y=self.u_y, std_acc=self.std_acc,
                                  y_std_meas=self.y_std_meas, x_std_meas=self.x_std_meas)
                self.trackIdCount += 1
                self.tracks.append(track)
                assignment.append(-1)
                print("Started new track:", self.tracks[-1].track_id)

        print("Number of detections M:   ", len(detections))
        print("Number of Tracks N:       ", len(self.tracks))
        print("Shape of cost matrix C: ", cost.shape)

        print("\nAssignment vector:        ", assignment, "\n")

        # Update KalmanFilter state, lastResults and tracks trace

        for i in range(len(self.tracks)):
            if i in del_tracks:
                continue
            if predicted_classes is not None:
                if i not in un_assigned_tracks:
                    self.tracks[i].predicted_class.append(predicted_classes[assignment[i]])
                else:
                    self.tracks[i].predicted_class.append("")
            if any(bounding_boxes) is not None:
                if i not in un_assigned_tracks:
                    self.tracks[i].bbox_trace.append(bounding_boxes[assignment[i]])
                else:
                    # if no new detection could be found, use the bounding box shape of the previous frame
                    self.tracks[i].bbox_trace.append(self.tracks[i].bbox_trace[-1])

            if self.use_kf:
                # Use Kalman Filter for track predictions
                self.tracks[i].KF.predict()

                if assignment[i] != -1:
                    self.tracks[i].skipped_frames = 0
                    self.tracks[i].prediction = self.tracks[i].KF.update(
                        detections[assignment[i]], 1)
                else:
                    if len(self.tracks[i].trace) > 1:
                        self.tracks[i].prediction = self.tracks[i].KF.update(
                            np.array([[0], [0]]), 0)

                if len(self.tracks[i].trace) > self.max_trace_length:
                    for j in range(len(self.tracks[i].trace) -
                                   self.max_trace_length):
                        del self.tracks[i].trace[j]

                self.tracks[i].trace.append(self.tracks[i].prediction[:2])
                self.tracks[i].KF.lastResult = self.tracks[i].prediction

            else:
                # No Kalman Filtering, just pure matching
                # only update the state of matched detections.
                # unmatched tracks will retain the same state as at t-1
                if assignment[i] != -1:
                    self.tracks[i].skipped_frames = 0
                    self.tracks[i].prediction = detections[assignment[i]]

                if len(self.tracks[i].trace) > self.max_trace_length:
                    for j in range(len(self.tracks[i].trace) -
                                   self.max_trace_length):
                        del self.tracks[i].trace[j]

                self.tracks[i].trace.append(self.tracks[i].prediction[:2])
